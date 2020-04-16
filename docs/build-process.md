# Build Process

1. User calls `buildContainer` XMLRPC call ― e.g. by `container-build` command
   of `rpkg`
1. Hub XMLRPC handler `buildContainer` creates `buildContainer` method
1. Builder method `buildContainer`
   1. Checks that target and SCM are correct
   1. Checks that build with given NVR doesn't exist (unless it's a scratch or
      autorelease task)
   1. For each architecture, [creates build in OSBS][]
   1. Watches logs and sends them to hub to save

[creates build in OSBS]: https://github.com/containerbuildsystem/koji-containerbuild/blob/master/koji_containerbuild/plugins/builder_containerbuild.py#L413