import datetime

import pytest

from healthconsole.core import yamlio


def test_reads_with_pyyaml_rules():
    data = yamlio.load("a: yes\nb: off\nc: 0777\nd: 1_000\ne: 2026-11-17\nf: ~\ng: 1e3\nh: 0x1f\nn: y\ns: 1:30\n")
    assert data == {"a": True, "b": False, "c": 511, "d": 1000, "e": datetime.date(2026, 11, 17), "f": None,
                    "g": "1e3", "h": 31, "n": "y", "s": 90}


def test_reads_bytes_as_utf8_and_empty_as_none():
    assert yamlio.load("name: Zoë\n".encode("utf-8")) == {"name": "Zoë"}
    assert yamlio.load("") is None


def test_dump_keeps_key_order_block_style_and_quotes_what_would_change_type():
    data = {"items": [{"name": "yes", "on": "0777", "z": 1, "a": [1, 2], "t": None}]}
    text = yamlio.dump(data)
    assert text == "items:\n- name: 'yes'\n  'on': '0777'\n  z: 1\n  a:\n  - 1\n  - 2\n  t: null\n"
    assert not text.startswith("%YAML")
    assert yamlio.load(text) == data


def test_bad_yaml_raises_yamlerror():
    with pytest.raises(yamlio.YAMLError):
        yamlio.load("a: [1")
