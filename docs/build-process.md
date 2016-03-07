1. User calls `buildContainer` XMLRPC call - e.g. by `container-build` command of `rpkg`.
2. Hub XMLRPC handler `buildContainer` creates `buildContainer` method.
3. Builder method `buildContainer`:
    1. [creates a build object](https://github.com/release-engineering/koji-containerbuild/blob/master/koji_containerbuild/plugins/builder_containerbuild.py#L642),
    2. subtask `createContainer`
    3. waits for `createcontainer` to finish
4. Builder method `createcontainer`:
    1. [creates build in OSBS](https://github.com/release-engineering/koji-containerbuild/blob/master/koji_containerbuild/plugins/builder_containerbuild.py#L333)
    2. Watches logs and sends them to hub to save.
    3. When build finishes downloads image tarball from the OSBS
5. Builder method `buildContainer`:
    1. Verifies list of rpms against database
    2. [Saves tarball as build artefact](https://github.com/release-engineering/koji-containerbuild/blob/master/koji_containerbuild/plugins/builder_containerbuild.py#L672)
    3. Creates subtask `tagBuild` to tag the build
