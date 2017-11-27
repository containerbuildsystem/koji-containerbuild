"""Module intended to be put into koji CLI to get container build commands"""

# Copyright (C) 2015  Red Hat, Inc.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
# USA

# Authors:
#       Pavol Babincak <pbabinca@redhat.com>

import os
from koji import _
from optparse import OptionParser

# Koji API has changed - activate_session requires two arguments
# _running_in_bg has been moved to koji_cli.lib
# parse_arches has been added to koji_cli.lib
try:
    from koji_cli.lib import _running_in_bg, activate_session, parse_arches
except ImportError:
    # Create wrappers for backwards compatibility.
    def _running_in_bg(*args, **kwargs):
        return clikoji._running_in_bg(*args, **kwargs)

    def parse_arches(arches):
        # Prior to being moved to koji_cli.lib, this used to be hard
        # coded like this:
        return ' '.join(arches.replace(',', ' ').split())

    def activate_session(session, options):
        try:
            clikoji.activate_session(session)
        except TypeError:
            clikoji.activate_session(session, options)


# Caller needs to set this to module which corresponds to /bin/koji
# This hack is here because koji CLI isn't a module but we need to use some of
# its functions. And this CLI don't necessary be named koji.
clikoji = None

# matches hub's buildContainer parameter channel
DEFAULT_CHANNEL = 'container'


def print_value(value, level, indent, suffix=''):
    offset = ' ' * level * indent
    print ''.join([offset, str(value), suffix])


def print_result(result, level=0, indent=2):
    if isinstance(result, list):
        for item in result:
            print_result(item, level+1)
    elif isinstance(result, dict):
        for key, value in result.items():
            print_value(key, level, indent, ':')
            print_result(value, level+1)
    else:
        print_value(result, level, indent)


def print_task_result(task_id, result, weburl):
    try:
        result["koji_builds"] = ["%s/buildinfo?buildID=%s" % (weburl, build_id)
                                 for build_id in result.get("koji_builds", [])]
    except TypeError:
        pass

    print "Task Result (%s):" % task_id
    print_result(result)


def parse_arguments(options, args, flatpak):
    "Build a container"
    if flatpak:
        usage = _("usage: %prog flatpak-build [options] target <scm url>")
    else:
        usage = _("usage: %prog container-build [options] target <scm url or "
                  "archive path>")
    usage += _("\n(Specify the --help global option for a list of other help "
               "options)")
    parser = OptionParser(usage=usage)
    if flatpak:
        parser.add_option("-m", "--module", metavar="NAME:STREAM[:VERSION]",
                          help="module to build against")
    parser.add_option("--scratch", action="store_true",
                      help=_("Perform a scratch build"))
    if not flatpak:
        parser.add_option("--isolated", action="store_true",
                          help=_("Perform an isolated build"))
    parser.add_option("--arch-override",
                      help=_("Requires --scratch. Limit a scratch build to "
                             "the specified arches. Comma or space separated."))
    parser.add_option("--wait", action="store_true",
                      help=_("Wait on the build, even if running in the "
                             "background"))
    parser.add_option("--nowait", action="store_false", dest="wait",
                      help=_("Don't wait on build"))
    parser.add_option("--quiet", action="store_true",
                      help=_("Do not print the task information"),
                      default=options.quiet)
    parser.add_option("--background", action="store_true",
                      help=_("Run the build at a lower priority"))
    parser.add_option("--epoch",
                      help=_("Specify container epoch. Requires koji admin "
                             "permission."))
    parser.add_option("--repo-url", dest='yum_repourls', metavar="REPO_URL",
                      action='append',
                      help=_("URL of yum repo file. May be used multiple "
                             "times. Cannot be used with --compose-id"))
    parser.add_option("--git-branch", metavar="GIT_BRANCH",
                      help=_("Git branch"))
    parser.add_option("--channel-override",
                      help=_("Use a non-standard channel [default: %default]"),
                      default=DEFAULT_CHANNEL)
    parser.add_option("--signing-intent",
                      help=_("Signing intent of the ODCS composes [default: %default]."
                             " Cannot be used with --compose-id"),
                      default=None, dest='signing_intent')
    parser.add_option("--compose-id",
                      help=_("ODCS composes used. May be used multiple times. Cannot be"
                             " used with --signing-intent or --repo-url"),
                      dest='compose_ids', action='append', metavar="COMPOSE_ID")
    if not flatpak:
        parser.add_option("--release",
                          help=_("Set release value"))
        parser.add_option("--koji-parent-build",
                          help=_("Overwrite parent image with image from koji build"))
    build_opts, args = parser.parse_args(args)
    if len(args) != 2:
        parser.error(_("Exactly two arguments (a build target and a SCM URL) "
                       "are required"))
        assert False

    if build_opts.arch_override and not build_opts.scratch:
        parser.error(_("--arch-override is only allowed for --scratch builds"))

    if build_opts.signing_intent and build_opts.compose_ids:
        parser.error(_("--signing-intent cannot be used with --compose-id"))

    if build_opts.compose_ids and build_opts.yum_repourls:
        parser.error(_("--compose-id cannot be used with --repo-url"))

    opts = {}
    if not build_opts.git_branch:
        parser.error(_("git-branch must be specified"))

    keys = ('scratch', 'epoch', 'yum_repourls', 'git_branch', 'signing_intent', 'compose_ids')

    if flatpak:
        if not build_opts.module:
            parser.error(_("module must be specified"))
        opts['flatpak'] = True
        keys += ('module',)
    else:
        if build_opts.isolated and build_opts.scratch:
            parser.error(_("Build cannot be both isolated and scratch"))

        keys += ('release', 'isolated', 'koji_parent_build')

    if build_opts.arch_override:
        opts['arch_override'] = parse_arches(build_opts.arch_override)

    for key in keys:
        val = getattr(build_opts, key)
        if val is not None:
            opts[key] = val
    # create the parser in this function and return it to
    # simplify the unit test cases
    return build_opts, args, opts, parser


def handle_build(options, session, args, flatpak):
    build_opts, args, opts, parser = parse_arguments(options, args, flatpak)

    activate_session(session, options)

    target = args[0]
    if target.lower() == "none" and build_opts.repo_id:
        target = None
        build_opts.skip_tag = True
    else:
        build_target = session.getBuildTarget(target)
        if not build_target:
            parser.error(_("Unknown build target: %s" % target))
        dest_tag = session.getTag(build_target['dest_tag'])
        if not dest_tag:
            parser.error(_("Unknown destination tag: %s" %
                           build_target['dest_tag_name']))
        if dest_tag['locked'] and not build_opts.scratch:
            parser.error(_("Destination tag %s is locked" % dest_tag['name']))
    source = args[1]

    priority = None
    if build_opts.background:
        # relative to koji.PRIO_DEFAULT
        priority = 5
    if '://' not in source:
        parser.error(_("scm URL does not look like an URL to a source repository"))
    if '#' not in source:
        parser.error(_("scm URL must be of the form <url_to_repository>#<revision>)"))
    task_id = session.buildContainer(source, target, opts, priority=priority,
                                     channel=build_opts.channel_override)
    if not build_opts.quiet:
        print "Created task:", task_id
        print "Task info: %s/taskinfo?taskID=%s" % (options.weburl, task_id)
    if build_opts.wait or (build_opts.wait is None and not _running_in_bg()):
        session.logout()
        rv = clikoji.watch_tasks(session, [task_id], quiet=build_opts.quiet)

        # Task completed and a result should be available.
        if rv == 0:
            result = session.getTaskResult(task_id)
            print_task_result(task_id, result, options.weburl)

        return rv
    else:
        return

def handle_container_build(options, session, args):
    "[build] Build a container"
    return handle_build(options, session, args, flatpak=False)

def handle_flatpak_build(options, session, args):
    "[build] Build a flatpak"
    return handle_build(options, session, args, flatpak=True)
