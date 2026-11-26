# Vendored front-end files

Served from `/static`; templates never reference a CDN.

| File | Package | Version | Source | Checksum |
|------|---------|---------|--------|----------|
| `htmx.min.js` | `htmx.org` (0BSD) | 2.0.4 | `package/dist/htmx.min.js` from https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.4.tgz (`npm pack htmx.org@2.0.4`) | tarball `sha512-HLxMCdfXDOJirs3vBZl/ZLoY+c7PfM4Ahr2Ad4YXh6d22T5ltbTXFFkpx9Tgb2vvmWFMbIc3LqN2ToNkZJvyYQ==` (matches npm `dist.integrity`); file sha256 `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `cytoscape.min.js` | `cytoscape` (MIT) | 3.34.3 | `package/dist/cytoscape.min.js` from https://registry.npmjs.org/cytoscape/-/cytoscape-3.34.3.tgz (`npm pack cytoscape@3.34.3`) | tarball `sha512-yfYGhRcGAntq6YBD583j4n0Eg3jIxvWmZtz/5uz9UYkeIStSlMxuUja+ec5j3iBD8nv1rwaOAYMW09tBdkSeaQ==` (matches npm `dist.integrity`); file sha256 `5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91` |
| `swagger-ui-bundle.js`, `swagger-ui.css` | `swagger-ui-dist` (Apache-2.0) | 5.34.1 | `swagger-ui-bundle.js` and `swagger-ui.css` from `package/` in https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.34.1.tgz (`npm pack swagger-ui-dist@5.34.1`) | tarball `sha512-VSr8fXfI93obYaQicLJbLUKGRxDojj/SfRJDDLCioGTUBj4BK/Nj4eE9jCeCAnJxVLi7iS+08PbiZLyrQKBkJQ==` (matches npm `dist.integrity`); file sha256 `36eeb4d5640b9c036b1d0f854499af1cceebbdb260b790a03a18be5e8488fbf5` (`swagger-ui-bundle.js`), `c3cfe8248c4b0b89673600fb829924eafbac6de4bc4b2b06f5f5243483074d0a` (`swagger-ui.css`) |

`app.css` and `topology.js` are project code, not vendored.

To update: `npm pack <package>@<version>`, check the tarball against `npm view <package>@<version> dist.integrity`, copy the `package/dist/` file named above here and update this table.
