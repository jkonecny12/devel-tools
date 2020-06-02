#!/usr/bin/python3

"""
reanaconda, a script to make testing different updates.img in QEMU faster.
This is achieved by separating the installation into two stages,
with a checkpoint before downloading updates.img.

First use `prime` to perform all steps preceeding updates.img downloading.
Then do `updates` to resumes/restart with an updated updates.img.

Requires Python 3.6+ (3.7+?), docopt, QEMU, netcat.

Usage:
  ./reanaconda.py prime [--tree <url>] [--append <extra_cmdline>] [--sensible]
                        <qemu_args>...
  ./reanaconda.py updates [<updates.img>]
  ./reanaconda.py cleanup
  ./reanaconda.py --help

Options:
  -h --help                 Show this help message.
  --tree <url>              Fetch files and install from a specific tree url.
  --sensible                Use some sensible preconfigured QEMU arguments.
  --append <extra_cmdline>  Extra cmdline arguments to append.
  <qemu_args>               Extra QEMU options to use.
  <updates.img>             An updates image to restart with.


Example session:
  $ ./reanaconda.py prime --sensible --tree http://.../x86_64/os
  $ # change something in an Anaconda checkout, then
  $ ./scripts/makeupdates
  $ ./reanaconda.py updates updates.img
  $ # or: echo updates.img | entr -r ./scripts/reanaconda updates updates.img
  $ ./reanaconda.py cleanup
"""

import http
import http.server
import os
import pickle
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import urllib.request

import docopt

QEMU_SENSIBLE_ARGUMENTS = [
    '-enable-kvm', '-machine', 'q35', '-cpu', 'host', '-smp', '2', '-m', '2G',
    '-object', 'rng-random,id=rng0,filename=/dev/urandom',
    '-device', 'virtio-rng-pci,rng=rng0',
    '-drive', 'file=reanaconda/disk.img,cache=unsafe,if=virtio',
]


class DaemonHTTPServer(http.server.HTTPServer, socketserver.ThreadingMixIn):
    daemon_threads = True


def start_a_503_server(callback):
    """
    Start an HTTP server on a random port
    that waits for a connection, replies with a 503 and executes a callback
    :param callback: a callback to execute on a connection
    :returns: port number where it's listening
    :rtype: int
    """
    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            raise RuntimeError('This server is not for serving any files')

        def handle(self):
            self.raw_requestline = self.rfile.readline(65537)
            self.requestline = str(self.raw_requestline, 'iso-8859-1').rstrip()
            self.parse_request()
            self.send_error(http.HTTPStatus.SERVICE_UNAVAILABLE,
                            'Service Unavailable (fake)')
            self.wfile.flush()
            self.wfile.close()
            callback()

    httpd = DaemonHTTPServer(('127.0.0.1', 0), Handler)
    _, port = httpd.socket.getsockname()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port


def start_a_single_file_server(path):
    """
    Start an HTTP server on a specified port
    that serves one file and one file only
    :param path: the path to the file to serve
    :param port: a port number to listen at
    :returns: port number where it's listening
    :rtype: int
    """
    class Handler(http.server.SimpleHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def translate_path(self, _unused_path):
            return path

    httpd = DaemonHTTPServer(('127.0.0.1', 0), Handler)
    _, port = httpd.socket.getsockname()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port


def find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _, port = s.getsockname()
    s.close()
    return port  # and hope it won't be claimed by someone else in the meantime


def _download(url, to):
    print(f'downloading {url}...')
    with urllib.request.urlopen(url) as r, open(to, 'wb') as f:
        shutil.copyfileobj(r, f)


class QEMU:
    def __init__(self, qemu_args, append):
        self.qemu_args = qemu_args
        if append:
            self.qemu_args += ['-append',
                               f'inst.updates=http://10.0.2.22 {append}']
        else:
            self.qemu_args += ['-append', 'inst.updates=http://10.0.2.22']

    def run(self, http_port, loadvm=None):
        self.monitor_port = find_free_port()
        cmd = ['qemu-system-x86_64'] + self.qemu_args + [
            '-monitor',
            f'tcp:127.0.0.1:{self.monitor_port},server,nowait,nodelay',
            '-device', 'virtio-net,netdev=net0', '-netdev',
            'user,id=net0,'
            f'guestfwd=tcp:10.0.2.22:80-cmd:nc 127.0.0.1 {http_port}'
        ]
        if loadvm:
            cmd += ['-loadvm', loadvm]
        print(f'executing {cmd}')
        subprocess.run(cmd, check=True)

    def monitor_execute(self, cmd, wait=True):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for i in range(40):
            try:
                sock.connect(('127.0.0.1', self.monitor_port))
                break
            except ConnectionRefusedError:
                time.sleep(.25)
        r = b''
        while r.count(b'(qemu)') < 1:
            r += sock.recv(64)
        sock.send((cmd + '\n').encode())
        if not wait:
            sock.recv(4096)
            return
        while r.count(b'(qemu)') < 2:
            r += sock.recv(64)
        sock.close()

    def pause(self):
        self.monitor_execute(f'stop')

    def savevm(self, snapshot_name):
        self.monitor_execute(f'savevm {snapshot_name}')

    def commit_all(self):
        self.monitor_execute(f'commit all')

    def quit(self):
        self.monitor_execute('quit', wait=False)


def prime(qemu_args, append, fetch_from=None):
    if os.path.isdir('reanaconda'):
        raise SystemExit('`reanaconda` dir already exists, `cleanup` first')
    os.makedirs('reanaconda')
    subprocess.run(['qemu-img', 'create', '-f', 'qcow2',
                    'reanaconda/disk.img', '20G'])

    if fetch_from:
        _download(f'{fetch_from}/isolinux/vmlinuz', 'reanaconda/vmlinuz')
        _download(f'{fetch_from}/isolinux/initrd.img', 'reanaconda/initrd.img')
        qemu_args += ['-kernel', 'reanaconda/vmlinuz',
                      '-initrd', 'reanaconda/initrd.img']
        append += f' inst.stage2={fetch_from}'

    qemu = QEMU(qemu_args, append)

    saving_done = threading.Event()

    def cb():
        time.sleep(.5)  # to make curl go into back-off
        qemu.pause()
        qemu.savevm('preupdates')
        qemu.commit_all()
        qemu.quit()
        with open('reanaconda/qemu.pickle', 'wb') as f:
            pickle.dump(qemu, f)
        saving_done.set()
        print('priming is done. resume with `reanaconda updates <updates.img>')
    http_port = start_a_503_server(cb)

    qemu.run(http_port)
    print('saved')
    saving_done.wait()
    print('exiting')


def updates(updates_img):
    if not os.path.isdir('reanaconda'):
        raise SystemExit('`reanaconda prime` first')
    with open('reanaconda/qemu.pickle', 'rb') as f:
        qemu = pickle.load(f)
    http_port = start_a_single_file_server(updates_img)
    time.sleep(.5)
    qemu.run(http_port, loadvm='preupdates')


def cleanup():
    if os.path.exists('reanaconda'):
        shutil.rmtree('reanaconda')


if __name__ == '__main__':
    args = docopt.docopt(__doc__, options_first=True)
    if args['prime']:
        append, tree, qemu_args = '', None, args['<qemu_args>']
        while (qemu_args and
               qemu_args[0] in ('--append', '--sensible', '--tree')):
            if qemu_args[0] == '--append':
                qemu_args.pop(0)
                append = qemu_args.pop(0)
            if qemu_args[0] == '--sensible':
                qemu_args.pop(0)
                qemu_args += QEMU_SENSIBLE_ARGUMENTS
            if qemu_args[0] == '--tree':
                qemu_args.pop(0)
                tree = qemu_args.pop(0)
        prime(qemu_args, append, fetch_from=tree)
    elif args['updates']:
        updates(args['<updates.img>'])
    elif args['cleanup']:
        cleanup()
