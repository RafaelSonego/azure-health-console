import threading
import time

from healthconsole.core import jobs, network, triage
from healthconsole.core.arm import Cancelled
from healthconsole.core.model import CRITICAL, HEALTHY, INFO, WARNING
from tests.conftest import SUB, FakeClient

VNET = f"/subscriptions/{SUB}/resourceGroups/net/providers/Microsoft.Network/virtualNetworks/hub"


def _wait(runner, job_id, owner="me"):
    for _ in range(200):
        job = runner.get(job_id, owner)
        if job["status"] != jobs.RUNNING:
            return job
        time.sleep(0.01)
    raise AssertionError("job never finished")


def test_cancel_stops_a_job_at_its_next_progress_update():
    runner = jobs.JobRunner()
    started = threading.Event()

    def work(progress, publish):
        started.set()
        for _ in range(500):
            progress("working")
            time.sleep(0.01)
        return "finished"

    job_id = runner.start("me", work)
    started.wait(1)
    runner.cancel(job_id, "me")
    assert _wait(runner, job_id)["status"] == jobs.CANCELLED


def test_starting_a_replacement_cancels_the_running_job_of_that_kind():
    runner = jobs.JobRunner()
    release = threading.Event()

    def slow(progress, publish):
        while not release.is_set():
            progress("waiting")
            time.sleep(0.01)
        return 1

    first = runner.start("me", slow, kind="snapshot", replace=True)
    other_owner = runner.start("you", slow, kind="snapshot", replace=True)
    second = runner.start("me", lambda progress, publish: 2, kind="snapshot", replace=True)
    assert _wait(runner, first)["status"] == jobs.CANCELLED
    assert _wait(runner, second)["result"] == 2
    assert runner.get(other_owner, "you")["status"] == jobs.RUNNING
    release.set()


def test_cancelled_is_not_swallowed_by_exception_guards():
    try:
        try:
            raise Cancelled()
        except Exception:  # noqa: BLE001 - what the per-signal guards do
            raise AssertionError("guard swallowed the cancellation")
    except Cancelled:
        pass


def _subnet_row(name, prefix, used=None, delegations=None, links=None):
    return {"vnetId": VNET, "vnet": "hub", "resourceGroup": "net", "subscriptionId": SUB, "subnet": name,
            "subnetId": f"{VNET}/subnets/{name}", "prefix": prefix, "prefixes": None, "used": used, "links": links,
            "nsg": f"/x/networkSecurityGroups/{name}-nsg", "routeTable": "", "delegations": delegations or []}


def _vnet_client(usages=None):
    vnet_row = {"id": VNET, "name": "hub", "resourceGroup": "net", "subscriptionId": SUB, "location": "canadacentral",
                "spaces": ["10.0.0.0/22"], "subnetCount": 3}
    subnets = [_subnet_row("aks", "10.0.0.0/24", used=240),
               _subnet_row("apps", "10.0.1.0/26", used=10),
               _subnet_row("web", "10.0.1.64/27",
                           delegations=[{"properties": {"serviceName": "Microsoft.Web/serverFarms"}}], links=1)]
    return FakeClient(query_handlers=[("mv-expand s = properties.subnets", lambda q: subnets),
                                      ("subnetCount", lambda q: [vnet_row])],
                      gets={"/usages": {"value": usages or []}})


def test_subnet_status_uses_the_thresholds():
    assert network.subnet_status(240, 251, False, 80, 90) == (CRITICAL, 95.6)
    assert network.subnet_status(210, 251, False, 80, 90)[0] == WARNING
    assert network.subnet_status(10, 59, False, 80, 90)[0] == HEALTHY
    assert network.subnet_status(None, 27, True, 80, 90) == (INFO, None)


def test_free_blocks_and_available_sizes():
    import ipaddress
    space = ipaddress.ip_network("10.0.0.0/22")
    used = [ipaddress.ip_network("10.0.0.0/24"), ipaddress.ip_network("10.0.1.0/26")]
    free = network.free_blocks(space, used)
    assert [str(f) for f in free] == ["10.0.1.64/26", "10.0.1.128/25", "10.0.2.0/23"]
    sizes = network.available_sizes([{"cidr": "10.0.0.0/22", "free_networks": free}])
    by_prefix = {r["prefix"]: r for r in sizes}
    assert by_prefix["/23"] == {"address_space": "10.0.0.0/22", "prefix": "/23", "next_cidr": "10.0.2.0/23",
                                "count": 1, "usable_ips": 507}
    assert by_prefix["/26"]["count"] == 11 and by_prefix["/26"]["next_cidr"] == "10.0.1.64/26"


def test_overview_ranks_vnets_by_their_fullest_subnet():
    result = network.overview(_vnet_client(), [SUB], None, 80, 90)
    vnet = result["vnets"][0]
    assert vnet["status"] == CRITICAL and vnet["subnets"] == 3 and vnet["problem_subnets"] == 1
    assert vnet["fullest"][0]["name"] == "aks" and vnet["allocated_percent"] == round(100 * 352 / 1024, 1)


def test_vnet_detail_prefers_azures_usage_list():
    usages = [{"id": f"{VNET}/subnets/apps", "currentValue": 55, "limit": 59}]
    detail = network.vnet_detail(_vnet_client(usages), VNET, 80, 90)
    subnets = {s["name"]: s for s in detail["subnets"]}
    assert subnets["apps"]["used"] == 55 and subnets["apps"]["status"] == CRITICAL
    assert subnets["web"]["status"] == INFO and "delegated" in subnets["web"]["note"]
    assert subnets["aks"]["nsg"] == "aks-nsg"
    assert detail["spaces"][0]["free_ranges"] == ["10.0.1.96/27", "10.0.1.128/25", "10.0.2.0/23"]
    assert detail["usage_source"] == "Azure's subnet usage list"


def test_triage_lists_subnets_running_low():
    items = triage._subnets(_vnet_client(), [SUB], "", {"subnet_warning_percent": 80, "subnet_critical_percent": 90})
    assert [i["name"] for i in items] == ["hub / aks"]
    assert items[0]["status"] == CRITICAL and "240 of 251" in items[0]["detail"]


def test_bench_groups_calls_by_kind_and_reports_each_scenario():
    from healthconsole.core import bench, config
    assert bench.endpoint("/subscriptions/a/resourceGroups/rg/providers/Microsoft.Web/sites/shop/detectors/x") \
        == "microsoft.web/sites/detectors"
    assert bench.endpoint("/providers/Microsoft.ResourceGraph/resources") == "microsoft.resourcegraph/resources"
    settings = config.load("/nonexistent")
    client = FakeClient(gets={"/subscriptions": {"value": [{"subscriptionId": SUB, "displayName": "s", "state": "Enabled"}]}})
    client._cache = type("C", (), {"clear": lambda self: None})()
    result = bench.run(client, settings, {"id": "t"}, ["triage", "resource"], say=lambda m: None)
    rows = {r["scenario"]: r for r in result["rows"]}
    assert "seconds" in rows["triage"] and rows["resource"]["skipped"]
    assert "| triage |" in bench.to_markdown(result)
