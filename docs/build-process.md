1. User calls `buildContainer` XMLRPC call - e.g. by `container-build` command of `rpkg`.
2. Hub XMLRPC handler `buildContainer` creates `buildContainer` method.
3. Builder method `buildContainer`:
    1. Checks that target and SCM are correct
    2. Checks that build with given NVR doesn't exist (unless its a scratch or autorelease task)
    3. For each architecture [creates build in OSBS](https://github.com/release-engineering/koji-containerbuild/blob/master/koji_containerbuild/plugins/builder_containerbuild.py#L413)
    4. Watches logs and sends them to hub to save.
