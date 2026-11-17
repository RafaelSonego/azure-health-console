# Bundled browser libraries

Shipped with the console so the page needs nothing from the internet (corporate
proxies often block public CDNs). Unmodified copies of the published builds.

| File | Library | Version | Licence | Source |
|---|---|---|---|---|
| `cytoscape-3.30.2.min.js` | Cytoscape.js | 3.30.2 | MIT | https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js |
| `chart-4.4.4.umd.min.js` | Chart.js | 4.4.4 | MIT | https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js |

To upgrade, replace the file, rename it with the new version, update the
`<script>` tags in `web/templates/index.html`, and this table.
