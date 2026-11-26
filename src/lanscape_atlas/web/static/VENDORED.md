# Vendored front-end files

Served from `/static`; templates never reference a CDN.

| File | Package | Version | Source | Checksum |
|------|---------|---------|--------|----------|
| `htmx.min.js` | `htmx.org` (0BSD) | 2.0.11 | `package/dist/htmx.min.js` from https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz (`npm pack htmx.org@2.0.11`) | tarball `sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==` (matches npm `dist.integrity`); file sha256 `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |
| `cytoscape.min.js` | `cytoscape` (MIT) | 3.34.5 | `package/dist/cytoscape.min.js` from https://registry.npmjs.org/cytoscape/-/cytoscape-3.34.5.tgz (`npm pack cytoscape@3.34.5`) | tarball `sha512-nmRD++h7D+AR9bcpggqoXQGKVA+K1SoKBSP1HkETQ3eMRzITegeA2GzNoB3DRSuHT5X8pOowwLCkNM4XAiNlQA==` (matches npm `dist.integrity`); file sha256 `e73eaa8c6206e95578604f4799454e66a09bf98bf318a09be4e68b7e225561af` |

`app.css` and `topology.js` are project code, not vendored.

To update: `npm pack <package>@<version>`, check the tarball against `npm view <package>@<version> dist.integrity`, copy the `package/dist/` file named above here and update this table.
