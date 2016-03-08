This document describes general concepts behind container builds architecture.
 
Let's say we have CLI named `rpkg` which is client of `pyrpkg` library. How koji configuration comes into container-build workflow:

    1. developer clones dist-git repository (`rpkg clone` command)

    2. developer switches branch (`rpkg switch-branch`). Let's say branch is `${BRANCH}`.

    3. in this branch there is a Dockerfile which contains couple of LABEL directives. One of them is `BZComponent`. Let's call this value ${COMPONENT}.

    4. developer wants to build the container so he issues `rpkg container-build` command.

Now some automagic comes into the game:

    1. `rpkg` constructs Koji target by appending `-docker-candidate` to branch name. In our case it is `${BRANCH}-docker-candidate`.

    2. during the build Koji buildroot tag is used as yum repository. *The
    semantic of buildroot tag differs from rpm builds* as it should contain
    either:

        - candidate (rpm) packages (to be released) - tags which ends `-candidate`.

        - released (rpm) packages - tags which doesn't have any suffix.

    3. Usually candidate nor released tags doesn't have architecture set in
    Koji. This means yum repository isn't generated for these tags. To
    workaround this we don't add architecture to these tags but create special
    "build" tag for containers. These builds tags has suffix `-container-build`
    and inherits from either candidate or released tag.

    4. After the container build succeed it is tagged to the destination tag.
    Due to Koji policy build needs to be added to (whitelisted in) this
    destination tag. Use `${COMPONENT}` to add the package to this destination
    tag.

This automagic is hardcoded in `pyrpkg` library but its clients (like `fedpkg`) can override this behaviour if needed.

Examples:

    $ rpkg co rsyslog-docker  
    $ cd ./rsyslog-docker  
    $ rpkg switch-branch extras-rhel-7.1  
    $ grep 'LABEL BZComponent' Dockerfile  
    LABEL BZComponent="rsyslog-docker"  
    $ brew list-targets --name extras-rhel-7.1-docker-candidate  
    Name                           Buildroot                      Destination                 
    ---------------------------------------------------------------------------------------------  
    extras-rhel-7.1-docker-candidate extras-rhel-7.1-container-build extras-rhel-7.1-candidate   
    $ brew taginfo extras-rhel-7.1-container-build | grep Arches  
    Arches: x86_64  
    $ brew list-tag-inheritance extras-rhel-7.1-container-build
    extras-rhel-7.1-container-build (8029)  
      ├─extras-rhel-7.1-candidate (6213)
      │  └─extras-rhel-7.1 (6210)  
      │     └─extras-rhel-7.0 (5868)  
      └─rhel-7.1-candidate (6221)  
         └─rhel-7.1-pending (6220)  
            └─rhel-7.1 (6219)  
               └─…  
    $ brew list-pkgs --tag extras-rhel-7.1 --package rsyslog-docker  
    Package                 Tag                     Extra Arches     Owner         
    ----------------------- ----------------------- ---------------- ---------------  
    rsyslog-docker          extras-rhel-7.1                          foo-owner
