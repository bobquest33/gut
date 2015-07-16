#!/usr/bin/env python

import argparse
import asyncio
import asyncssh
import os
from queue import Queue
import sys
import time
import traceback

import plumbum
from . import patch_plumbum; patch_plumbum.patch()

from . import config
from . import terminal as term
from .terminal import shutdown, color_host_path, color_commit, Writer, on_shutdown, quote_proc
from . import deps
from . import gut_cmd
from . import gut_build
from . import util

@asyncio.coroutine
def ensure_build(context):
    status = Writer(context)
    desired_git_version = config.GIT_WIN_VERSION if context._is_windows else config.GIT_VERSION
    if not gut_cmd.exe_path(context).exists() or desired_git_version.lstrip('v') not in gut_cmd.get_version(context):
        status.out('(@dim)Need to build gut on ' + context._name_ansi + '(@dim).(@r)\n')
        gut_build.ensure_gut_folders(context)
        yield from gut_build.prepare(context)
        if context != plumbum.local:
            # If we're building remotely, rsync the prepared source to the remote host
            build_path = config.GUT_SRC_TMP_PATH
            yield from util.rsync(plumbum.local, config.GUT_SRC_PATH, context, build_path, excludes=['.git', 't'])
        else:
            build_path = config.GUT_WIN_SRC_PATH if context._is_windows else config.GUT_SRC_PATH
        yield from gut_build.build(context, build_path)
        status.out('(@dim)Cleaning up...(@r)')
        yield from gut_build.unprepare(context)
        if context != plumbum.local:
            context['rm']['-r', context.path(config.GUT_SRC_TMP_PATH)]()
        status.out('(@dim) done.(@r)\n')
        return True
    return False

def get_tail_hash(context, sync_path):
    """
    Query the gut repo for the initial commit to the repo. We use this to determine if two gut repos are compatibile.
    http://stackoverflow.com/questions/1006775/how-to-reference-the-initial-commit
    """
    path = context.path(sync_path)
    if (path / '.gut').exists():
        with context.cwd(path):
            return gut_cmd.gut(context)['rev-list', '--max-parents=0', 'HEAD'](retcode=None).strip() or None
    return None

def assert_folder_empty(context, _path):
    path = context.path(_path)
    if path.exists() and ((not path.isdir()) or len(path.list()) > 0):
        # If it exists, and it's not a directory or not an empty directory, then bail
        status = Writer(context)
        status.out('(@error)Refusing to initialize (@path)%s (@error) on %s' % (path, context._name_ansi))
        status.out('(@error)as it is not an empty directory. Move or delete it manually first.\n')
        shutdown()

def init_context(context, sync_path=None, host=None, user=None):
    context._name = host or 'localhost'
    context._name_ansi = '(@host)%s(@r)' % (context._name,)
    context._is_local = not host
    context._is_osx = context.uname == 'Darwin'
    context._is_linux = context.uname == 'Linux'
    context._is_windows = context.uname == 'Windows'
    context._ssh_address = (('%s@' % (user,) if user else '') + host) if host else ''
    context._sync_path = color_host_path(context, sync_path)
    if context._is_osx:
        # Because .profile vs .bash_profile vs .bashrc is probably not right, and this is where homebrew installs stuff, by default
        context.env['PATH'] = context.env['PATH'] + ':/usr/local/bin'
    if context._is_windows:
        context.env.path.append(context.path(config.INOTIFY_WIN_PATH))

class MySSHClientSession(asyncssh.SSHClientSession):
    def data_received(self, data, datatype):
        status.out('data_received: [%r], datatype: [%r]\n' % (data, datatype))

    def connection_lost(self, exc):
        if exc:
            print('SSH session error: ' + str(exc), file=sys.stderr)

class MySSHClient(asyncssh.SSHClient):
    def connection_made(self, conn):
        print('Connection made to %s.' % conn.get_extra_info('peername')[0])

    def auth_completed(self):
        print('Authentication successful.')

class AsyncSshPopen(object):
    def __init__(self, argv):
        self.argv = argv
    def poll(self):
        raise NotImplementedError()
    def wait(self):
        raise NotImplementedError()
    def close(self):
        raise NotImplementedError()
    def kill(self):
        raise NotImplementedError()
    terminate = kill
    def send_signal(self, sig):
        raise NotImplementedError()
    def communicate(self):
        raise NotImplementedError()

class AsyncSshMachine(plumbum.machines.remote.BaseRemoteMachine):
    def __init__(self, host, user=None, port=None, password=None, keyfile=None,
                 load_system_host_keys=True, missing_host_policy=None, encoding="utf8",
                 look_for_keys=None, connect_timeout=None, keep_alive=0):
        self.host = host
        self.user = user
        self.encoding = encoding
        self.connect_timeout = connect_timeout
        plumbum.machines.remote.BaseRemoteMachine.__init__(self, self.encoding, self.connect_timeout)

    @asyncio.coroutine
    def init(self):
        self._conn, self._client = yield from asyncssh.create_connection(MySSHClient, self.host, username=self.user)

    def session(self, isatty=False, term="vt100", width=80, height=24, new_session=False):
        proc = AsyncSshPopen(["<shell>"])
        return plumbum.machines.session.ShellSession(proc, self.encoding, isatty)

    @asyncio.coroutine
    def popen(self):
        chan, session = yield from self._conn.create_session(MySSHClientSession, '>&2 echo stderr; sleep 1; echo stdout')
        yield from chan.wait_closed()

class SyncContext:
    REMOTE_CONN = 'asyncssh'

    def __init__(self, path=None, host=None, user=None, keyfile=None):
        self.host = host
        self.user = user
        self.path = path
        self.keyfile = keyfile

    @classmethod
    @asyncio.coroutine
    def make(cls, *args, **kwargs):
        me = SyncContext(*args, **kwargs)
        yield from me.init()
        return me

    @asyncio.coroutine
    def init(self):
        self.conn = yield from self.make_conn()

    @asyncio.coroutine
    def make_conn(self):
        if not self.host:
            conn = plumbum.local
        elif SyncContext.REMOTE_CONN == 'openssl':
            conn = plumbum.SshMachine(
                self.host,
                user=self.user,
                keyfile=self.keyfile)
        elif SyncContext.REMOTE_CONN == 'paramiko':
            # Import paramiko late so that one could use `--use-openssl` without even installing paramiko
            import paramiko
            from plumbum.machines.paramiko_machine import ParamikoMachine
            # XXX paramiko doesn't seem to successfully update my known_hosts file with this setting
            conn = ParamikoMachine(
                self.host,
                user=self.user,
                keyfile=self.keyfile,
                missing_host_policy=paramiko.AutoAddPolicy())
        elif SyncContext.REMOTE_CONN == 'asyncssh':
            conn = AsyncSshMachine(
                self.host,
                user=self.user,
                keyfile=self.keyfile)
            yield from conn.init()
        init_context(conn, sync_path=self.path, host=self.host, user=self.user)
        return conn


@asyncio.coroutine
def sync(local_path, remote_user, remote_host, remote_path, keyfile=None):
    local = yield from SyncContext.make(path=local_path)
    remote = yield from SyncContext.make(path=remote_path, host=remote_host, user=remote_user, keyfile=keyfile)

    status = Writer(local.conn, 'gut-sync')

    # session = remote.conn.session()
    # asyncio.get_event_loop().call_soon(lambda: session.popen('pwd'))
    # yield from quote_proc(remote.conn, 'test', session.proc)

    # return

    yield from remote.conn['pwd'].run()



    return

    # with (yield from asyncssh.connect(remote_host, username=remote_user)) as conn:
    #     stdin, stdout, stderr = yield from conn.open_session('pwd')
    #     asyncio.async(Writer(remote, 'out').quote_fd(stdout))
    #     yield from Writer(remote, 'out').quote_fd(stderr)

    yield from quote_proc(remote.conn, 'test', remote.conn['pwd'].popen())

    return
    status.out('(@dim)Syncing ' + local._sync_path + ' (@dim)with ' + remote._sync_path + '\n')

    ports = util.find_open_ports([local, remote], 3)
    # out(dim('Using ports ') + dim(', ').join([unicode(port) for port in ports]) +'\n')
    gutd_bind_port, gutd_connect_port, autossh_monitor_port = ports

    yield from ensure_build(local)
    yield from ensure_build(remote)

    local_tail_hash = get_tail_hash(local, local_path)
    remote_tail_hash = get_tail_hash(remote, remote_path)
    tail_hash = None

    yield from util.start_ssh_tunnel(local, remote, gutd_bind_port, gutd_connect_port, autossh_monitor_port)

    @asyncio.coroutine
    def cross_init(src_context, src_path, dest_context, dest_path):
        yield from gut_cmd.daemon(src_context, src_path, tail_hash, gutd_bind_port)
        yield from gut_cmd.init(dest_context, dest_path)
        gut_cmd.setup_origin(dest_context, dest_path, tail_hash, gutd_connect_port)
        import time
        time.sleep(2) # Give the gut-daemon and SSH tunnel a moment to start up
        yield from gut_cmd.pull(dest_context, dest_path)
        yield from gut_cmd.daemon(dest_context, dest_path, tail_hash, gutd_bind_port)

    # Do we need to initialize local and/or remote gut repos?
    if not local_tail_hash or local_tail_hash != remote_tail_hash:
        status.out('(@dim)Local gut repo base commit: [' + color_commit(local_tail_hash) + '(@dim)]\n')
        status.out('(@dim)Remote gut repo base commit: [' + color_commit(remote_tail_hash) + '(@dim)]\n')
        if local_tail_hash and not remote_tail_hash:
            tail_hash = local_tail_hash
            assert_folder_empty(remote, remote_path)
            status.out('(@dim)Initializing remote repo from local repo...\n')
            yield from cross_init(local, local_path, remote, remote_path, )
        elif remote_tail_hash and not local_tail_hash:
            tail_hash = remote_tail_hash
            assert_folder_empty(local, local_path)
            status.out('(@dim)Initializing local folder from remote gut repo...\n')
            yield from cross_init(remote, remote_path, local, local_path)
        elif not local_tail_hash and not remote_tail_hash:
            assert_folder_empty(remote, remote_path)
            assert_folder_empty(local, local_path)
            status.out('(@dim)Initializing both local and remote gut repos...\n')
            status.out('(@dim)Initializing local repo first...\n')
            yield from gut_cmd.init(local, local_path)
            yield from gut_cmd.ensure_initial_commit(local, local_path)
            tail_hash = get_tail_hash(local, local_path)
            status.out('(@dim)Initializing remote repo from local repo...\n')
            yield from cross_init(local, local_path, remote, remote_path)
        else:
            status.out('(@error)Cannot sync incompatible gut repos:\n')
            status.out('(@error)Local initial commit hash: [%s(@error)]\n' % (color_commit(local_tail_hash),))
            status.out('(@error)Remote initial commit hash: [%s(@error)]\n' % (color_commit(remote_tail_hash),))
            shutdown()
    else:
        tail_hash = local_tail_hash
        yield from gut_cmd.daemon(local, local_path, tail_hash, gutd_bind_port)
        yield from gut_cmd.daemon(remote, remote_path, tail_hash, gutd_bind_port)
        # XXX The gut daemons are not necessarily listening yet, so this could result in races with commit_and_update calls below

    gut_cmd.setup_origin(local, local_path, tail_hash, gutd_connect_port)
    gut_cmd.setup_origin(remote, remote_path, tail_hash, gutd_connect_port)

    @asyncio.coroutine
    def commit_and_update(src_system, changed_paths=None, update_untracked=False):
        if src_system == 'local':
            src_context = local
            src_path = local_path
            dest_context = remote
            dest_path = remote_path
            dest_system = 'remote'
        else:
            src_context = remote
            src_path = remote_path
            dest_context = local
            dest_path = local_path
            dest_system = 'local'

        # Based on the set of changed paths, figure out what we need to pass to `gut add` in order to capture everything
        if not changed_paths:
            prefix = '.'
        # This is kind of annoying because it regularly picks up .gutignored files, e.g. the ".#." files emacs drops:
        # elif len(changed_paths) == 1:
        #     (prefix,) = changed_paths
        else:
            # commonprefix operates on strings, not paths; so lop off the last bit of the path so that if we get two files within
            # the same directory, e.g. "test/sarah" and "test/sally", we'll look in "test/" instead of in "test/sa".
            separator = '\\' if src_context._is_windows else '/'
            prefix = os.path.commonprefix(changed_paths).rpartition(separator)[0] or '.'
        # out('system: %s\npaths: %s\ncommon prefix: %s\n' % (src_system, ' '.join(changed_paths) if changed_paths else '', prefix))

        try:
            if (yield from gut_cmd.commit(src_context, src_path, prefix, update_untracked=update_untracked)):
                yield from gut_cmd.pull(dest_context, dest_path)
        except plumbum.commands.ProcessExecutionError:
            status.out('\n\n(@error)Error during commit-and-pull:\n')
            traceback.print_exc(file=sys.stderr)

    event_queue = asyncio.Queue()
    SHUTDOWN_STR = '3qo4c8h56t349yo57yfv534wto8i7435oi5'
    def shutdown_watch_consumer():
        @asyncio.coroutine
        def _shutdown_watch_consumer():
            yield from event_queue.put(SHUTDOWN_STR)
        asyncio.async(_shutdown_watch_consumer())
    on_shutdown(shutdown_watch_consumer)

    yield from util.watch_for_changes(local, local_path, 'local', event_queue)
    yield from util.watch_for_changes(remote, remote_path, 'remote', event_queue)
    # The filesystem watchers are not necessarily listening to all updates yet, so we could miss file changes that occur between the
    # commit_and_update calls below and the time that the filesystem watches are attached.

    yield from commit_and_update('remote', update_untracked=True)
    yield from commit_and_update('local', update_untracked=True)
    yield from gut_cmd.pull(remote, remote_path)
    yield from gut_cmd.pull(local, local_path)

    changed = {}
    changed_ignore = set()
    while True:
        try:
            fut = event_queue.get()
            event = yield from (asyncio.wait_for(fut, 0.1) if changed else fut)
        except asyncio.TimeoutError:
            for system, paths in changed.items():
                yield from commit_and_update(system, paths, update_untracked=(system in changed_ignore))
            changed.clear()
            changed_ignore.clear()
        else:
            if event == SHUTDOWN_STR:
                break
            system, path = event
            # Ignore events inside the .gut folder; these should also be filtered out in inotifywait/fswatch/etc if possible
            path_parts = path.split(os.sep)
            if not '.gut' in path_parts:
                if system not in changed:
                    changed[system] = set()
                changed[system].add(path)
                if path_parts[-1] == '.gutignore':
                    changed_ignore.add(system)
                    status.out('changed_ignore %s on %s\n' % (path, system))
                else:
                    status.out('changed %s %s\n' % (system, path))
            else:
                status.out('ignoring changed %s %s\n' % (system, path))

@asyncio.coroutine
def main_coroutine():
    status = Writer(None)
    action = len(sys.argv) >= 2 and sys.argv[1]
    if action in config.ALL_GUT_COMMANDS:
        local = plumbum.local
        init_context(local)
        gut_exe_path = str(gut_cmd.exe_path(local))
        # Build gut if needed
        if not os.path.exists(gut_exe_path):
            yield from ensure_build(local)
        os.execv(gut_exe_path, [gut_exe_path] + sys.argv[1:])
    else:
        if '--version' in sys.argv:
            import pkg_resources
            print('gut-sync version %s' % (pkg_resources.require("gut")[0].version,))
            return
        parser = argparse.ArgumentParser()
        parser.add_argument('action', choices=['build', 'sync'])
        parser.add_argument('--install-deps', action='store_true')
        parser.add_argument('--no-color', action='store_true')
        parser.add_argument('--use-openssl', action='store_true')
        def parse_args():
            args = parser.parse_args()
            deps.auto_install = args.install_deps
            if args.use_openssl:
                SyncContext.REMOTE_CONN = 'openssl'
            if args.no_color:
                term.disable_color()
            return args
        if action == 'build':
            args = parser.parse_args()
            local = plumbum.local
            init_context(local)
            if not (yield from ensure_build(local)):
                status.out('(@dim)gut ' + config.GIT_VERSION + '(@dim) has already been built.\n')
        else:
            parser.add_argument('local')
            parser.add_argument('remote')
            parser.add_argument('--identity', '-i')
            parser.add_argument('--dev', action='store_true')
            # parser.add_argument('--verbose', '-v', action='count')
            args = parser.parse_args()
            local_path = args.local
            if ':' not in args.remote:
                parser.error('remote must include both the hostname and path, separated by a colon')
            if args.dev:
                asyncio.async(util.restart_on_change(os.path.abspath(__file__)))
            remote_addr, remote_path = args.remote.split(':', 1)
            remote_user, remote_host = remote_addr.rsplit('@', 2) if '@' in remote_addr else (None, remote_addr)
            yield from sync(local_path, remote_user, remote_host, remote_path, keyfile=args.identity)

init_context(plumbum.local)
# tick_writer = Writer(plumbum.local, 'tick')
# @asyncio.coroutine
# def tick():
#     yield from asyncio.sleep(0.5)
#     tick_writer.out('tock\n')
#     asyncio.async(tick())

def main():
    try:
        # asyncio.async(tick())
        asyncio.get_event_loop().run_until_complete(main_coroutine())
    except SystemExit:
        shutdown(exit=False)
        raise
    except KeyboardInterrupt:
        Writer(None).out('\n(@dim)SIGINT received. Shutting down...')
    shutdown(exit=False)

if __name__ == '__main__':
    main()
