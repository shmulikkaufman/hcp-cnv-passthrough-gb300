#!/usr/bin/env python3
"""GPUDirect RDMA data-content integrity test (dma-buf path, inbox mlx5 stack).

Server:  allocates GPU memory (CUDA VMM), exports dmabuf FD, registers DmaBufMR,
         receives RDMA WRITEs from client, reads GPU memory back via cuMemcpyDtoH
         and verifies byte-for-byte positional patterns.
Client:  allocates its own GPU memory the same way, fills positional pattern,
         RDMA-writes it to the server's GPU buffer in 1 MiB chunks.

Both sides exchange QP info over a plain TCP socket on the rail subnet.
"""
import argparse
import ctypes
import json
import os
import random
import socket
import sys
import time

# ---------------- CUDA driver API via ctypes ----------------
CUDA_OK = 0
CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR = 0x1
CU_MEM_ALLOCATION_TYPE_PINNED = 1
CU_MEM_LOCATION_TYPE_DEVICE = 1
CU_MEM_ACCESS_FLAGS_PROT_READWRITE = 0x3
CU_MEM_ALLOC_GRANULARITY_MINIMUM = 0

TILE = 65536  # 64 KiB pattern tile


class CUmemLocation(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('id', ctypes.c_int)]


class CUmemAllocationProp(ctypes.Structure):
    class _AllocFlags(ctypes.Structure):
        _fields_ = [('compressionType', ctypes.c_ubyte),
                    ('gpuDirectRdma', ctypes.c_ubyte),
                    ('usage', ctypes.c_ubyte),
                    ('reserved', ctypes.c_ubyte * 6)]
    _fields_ = [('type', ctypes.c_int),
                ('requestedHandleTypes', ctypes.c_uint),
                ('location', CUmemLocation),
                ('win32HandleMetaData', ctypes.c_void_p),
                ('allocFlags', _AllocFlags)]


class CUmemAccessDesc(ctypes.Structure):
    _fields_ = [('location', CUmemLocation), ('flags', ctypes.c_uint)]


class GpuMem:
    """CUDA VMM allocation exported as dmabuf FD (perftest-equivalent flow)."""

    def __init__(self, size_mib):
        self.cu = ctypes.CDLL('libcuda.so.1')
        self._proto()
        rc = self.cu.cuInit(0)
        self._check(rc, 'cuInit')
        dev = ctypes.c_uint()
        rc = self.cu.cuDeviceGet(ctypes.byref(dev), 0)
        self._check(rc, 'cuDeviceGet')
        self.dev = dev
        ctx = ctypes.c_void_p()
        rc = self.cu.cuCtxCreate(ctypes.byref(ctx), 0, dev)
        self._check(rc, 'cuCtxCreate')
        self.ctx = ctx
        name = ctypes.create_string_buffer(128)
        rc = self.cu.cuDeviceGetName(name, 128, dev)
        if rc == CUDA_OK:
            print('CUDA device 0: %s' % name.value.decode())

        self.size = size_mib * 1024 * 1024
        # perftest-equivalent flow: cuMemAlloc(_v2) + page-aligned
        # dmabuf export of the covering range
        ptr = ctypes.c_uint64()
        rc = self.cu.cuMemAlloc(ctypes.byref(ptr), self.size)
        self._check(rc, 'cuMemAlloc')
        self.va = ptr.value
        ps = os.sysconf('SC_PAGESIZE')
        aligned = self.va & ~(ps - 1)
        self.offset = self.va - aligned
        self.alloc_size = (self.size + self.offset + ps - 1) & ~(ps - 1)
        print('GPU alloc: va=0x%x size=%d pagesize=%d dmabuf_offset=%d'
              % (self.va, self.size, ps, self.offset), flush=True)
        fdh = ctypes.c_uint64()
        rc = self.cu.cuMemGetHandleForAddressRange(
            ctypes.byref(fdh), aligned, self.alloc_size,
            CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR, 0)
        self._check(rc, 'cuMemGetHandleForAddressRange')
        self.fd = int(fdh.value)
        print('dmabuf export: aligned=0x%x aligned_size=%d fd=%d'
              % (aligned, self.alloc_size, self.fd), flush=True)

    def _sym(self, name):
        """Resolve a CUDA driver symbol, preferring the current versioned ABI.

        Unversioned exports (cuMemAlloc, cuMemcpyHtoD, ...) are legacy v1
        entry points in libcuda.so.1; the real APIs are cuMemAlloc_v2 etc.
        (cuda.h #defines confirm the mapping).
        """
        versioned = {'cuCtxCreate': 'cuCtxCreate_v2',
                     'cuMemAlloc': 'cuMemAlloc_v2',
                     'cuMemcpyHtoD': 'cuMemcpyHtoD_v2',
                     'cuMemcpyDtoH': 'cuMemcpyDtoH_v2'}
        if name in versioned:
            try:
                return getattr(self.cu, versioned[name])
            except AttributeError:
                pass
        return getattr(self.cu, name)

    def _proto(self):
        self.cu.cuInit.argtypes = [ctypes.c_uint]
        self.cu.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_uint), ctypes.c_int]
        self.cu.cuCtxCreate = self._sym('cuCtxCreate')
        self.cu.cuCtxCreate.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_uint]
        self.cu.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]
        self.cu.cuMemAlloc = self._sym('cuMemAlloc')
        self.cu.cuMemAlloc.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t]
        self.cu.cuMemGetHandleForAddressRange.argtypes = [
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint64, ctypes.c_size_t, ctypes.c_uint, ctypes.c_uint64]
        self.cu.cuMemcpyHtoD = self._sym('cuMemcpyHtoD')
        self.cu.cuMemcpyHtoD.argtypes = [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t]
        self.cu.cuMemcpyDtoH = self._sym('cuMemcpyDtoH')
        self.cu.cuMemcpyDtoH.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t]

    def _check(self, rc, what):
        if rc != CUDA_OK:
            raise RuntimeError('CUDA error %d in %s' % (rc, what))

    def fill(self, pattern_tile):
        """Fill whole GPU buffer by repeating a 64 KiB host tile."""
        for off in range(0, self.size, TILE):
            rc = self.cu.cuMemcpyHtoD(self.va + off, pattern_tile,
                                      min(TILE, self.size - off))
            self._check(rc, 'cuMemcpyHtoD')

    def readback(self):
        buf = (ctypes.c_ubyte * self.size)()
        rc = self.cu.cuMemcpyDtoH(buf, self.va, self.size)
        self._check(rc, 'cuMemcpyDtoH')
        return memoryview(buf)


def make_tile(magic):
    b = bytearray(TILE)
    for i in range(TILE):
        b[i] = (magic + (i & 0xFF) * 3 + (i >> 8) * 11 + (i >> 16) * 7) & 0xFF
    return bytes(b)


def verify(mv, magic):
    t = make_tile(magic)
    bad_tiles = 0
    first = None
    for k in range(0, len(mv), TILE):
        seg = mv[k:k + TILE]
        if seg != t:
            bad_tiles += 1
            if first is None:
                for j in range(TILE):
                    if seg[j] != t[j]:
                        first = (k + j, seg[j], t[j])
                        break
    return bad_tiles, first


# ---------------- pyverbs RDMA ----------------
from pyverbs.device import Context
from pyverbs.pd import PD
from pyverbs.cq import CQ
from pyverbs.qp import QP, QPAttr, QPInitAttr, QPCap
from pyverbs.mr import DmaBufMR
from pyverbs.addr import AHAttr, GRH, GID
from pyverbs.wr import SendWR, SGE
from pyverbs.libibverbs_enums import (IBV_WR_RDMA_WRITE, IBV_SEND_SIGNALED,
                                      IBV_QPT_RC, IBV_MTU_4096,
                                      IBV_ACCESS_LOCAL_WRITE, IBV_ACCESS_REMOTE_WRITE)

CHUNK = 1024 * 1024


class RdmaEp:
    def __init__(self, dev='mlx5_1', gid_index=3):
        self.ctx = Context(name=dev)
        self.pd = PD(self.ctx)
        self.cq = CQ(self.ctx, 128)
        cap = QPCap(max_send_wr=64, max_recv_wr=64, max_send_sge=4, max_recv_sge=4)
        attr = QPInitAttr(qp_type=IBV_QPT_RC, scq=self.cq, rcq=self.cq, cap=cap)
        self.qp = QP(self.pd, attr)
        self.gid_index = gid_index
        self.gid = self.ctx.query_gid(1, gid_index)
        self.psn = random.randint(0, 0xffffff)

    def reg_mr(self, gpu, access):
        self.mr = DmaBufMR(self.pd, gpu.size, access, dmabuf=gpu.fd,
                           offset=gpu.offset)
        return self.mr

    def info(self):
        return {'qp_num': self.qp.qp_num, 'psn': self.psn, 'rkey': self.mr.rkey,
                'gid': str(self.gid), 'gid_index': self.gid_index}

    def connect(self, remote):
        a = QPAttr()
        a.port_num = 1
        a.qp_access_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE
        self.qp.to_init(a)
        ah = AHAttr()
        ah.is_global = 1
        ah.port_num = 1
        ah.sgid_index = self.gid_index
        ah.dgid = remote['gid']  # dgid setter takes the gid string
        a = QPAttr()
        a.dest_qp_num = remote['qp_num']
        a.rq_psn = remote['psn']
        a.path_mtu = IBV_MTU_4096
        a.min_rnr_timer = 0x12
        a.max_dest_rd_atomic = 1
        a.port_num = 1
        a.ah_attr = ah
        self.qp.to_rtr(a)
        a = QPAttr()
        a.sq_psn = self.psn
        a.timeout = 0x12
        a.retry_cnt = 7
        a.rnr_retry = 7
        a.max_rd_atomic = 1
        self.qp.to_rts(a)

    def rdma_write(self, remote_addr, rkey, length, local_addr):
        sge = SGE(addr=local_addr, length=length, lkey=self.mr.lkey)
        wr = SendWR(opcode=IBV_WR_RDMA_WRITE, sg=[sge], num_sge=1,
                    send_flags=IBV_SEND_SIGNALED)
        wr.set_wr_rdma(rkey=rkey, addr=remote_addr)
        self.qp.post_send(wr)
        return self.wait_ok()

    def wait_ok(self, deadline=30.0):
        if not hasattr(self, '_pending'):
            from collections import deque
            self._pending = deque()
        t0 = time.time()
        while time.time() - t0 < deadline:
            if self._pending:
                wc = self._pending.popleft()
                if wc.status != 0:
                    return ('ERR', wc.status)
                return ('OK', None)
            n, wcs = self.cq.poll(16)
            for i in range(n):
                self._pending.append(wcs[i])
        try:
            q = self.qp.query()
            return ('POLL_TIMEOUT', 'qp_state=%s outstanding=%d'
                    % (q.cur_qp_state if hasattr(q, 'cur_qp_state') else '?',
                       len(self._pending)))
        except Exception as e:
            return ('POLL_TIMEOUT', repr(e))

    def close(self):
        for obj in ('qp', 'mr', 'cq', 'pd', 'ctx'):
            o = getattr(self, obj, None)
            if o is not None:
                try:
                    o.close()
                except Exception:
                    pass
                setattr(self, obj, None)


def send_json(sock, obj):
    sock.sendall((json.dumps(obj) + '\n').encode())


def recv_json(sock):
    buf = b''
    while not buf.endswith(b'\n'):
        d = sock.recv(65536)
        if not d:
            raise ConnectionError('peer closed')
        buf += d
    return json.loads(buf)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('role', choices=['server', 'client'])
    p.add_argument('ip')
    p.add_argument('tcp_port', type=int)
    p.add_argument('--size-mib', type=int, default=32)
    p.add_argument('--rounds', type=int, default=3)
    p.add_argument('--dev', default='mlx5_1')
    p.add_argument('--gid-index', type=int, default=3)
    args = p.parse_args()

    import gc
    gc.disable()  # pyverbs teardown during cyclic GC can segfault mid-run

    random.seed(os.getpid() ^ int(time.time()))
    gpu = GpuMem(args.size_mib)
    ep = RdmaEp(args.dev, args.gid_index)

    if args.role == 'server':
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((args.ip, args.tcp_port))
        srv.listen(1)
        print('server: listening on %s:%d' % (args.ip, args.tcp_port), flush=True)
        conn, addr = srv.accept()
        print('server: client connected from %s' % (addr,), flush=True)
        # initial pattern + readback sanity
        magic0 = 0xC3
        gpu.fill(make_tile(magic0))
        bad, first = verify(gpu.readback(), magic0)
        print('server: initial GPU fill/readback sanity: %s (bad_tiles=%d)'
              % ('PASS' if bad == 0 else 'FAIL', bad), flush=True)
        rounds_done = 0
        while True:
            msg = recv_json(conn)
            if msg['cmd'] == 'hello':
                ep.close()
                ep = RdmaEp(args.dev, args.gid_index)
                ep.reg_mr(gpu, IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE)
                remote = msg['remote']
                ep.connect(remote)
                send_json(conn, {'cmd': 'hello_ack', **ep.info()})
                print('server: QP established (qp_num=0x%x)' % ep.qp.qp_num, flush=True)
            elif msg['cmd'] == 'verify':
                t0 = time.time()
                bad, first = verify(gpu.readback(), msg['magic'])
                dt = time.time() - t0
                ok = bad == 0 and first is None
                print('server: round %d magic=0x%02x verify: %s bad_tiles=%d/%d '
                      'first_mismatch=%s (readback %.2fs)'
                      % (msg['round'], msg['magic'], 'PASS' if ok else 'FAIL',
                         bad, gpu.size // TILE, first, dt), flush=True)
                send_json(conn, {'cmd': 'verify_ack', 'ok': ok, 'bad_tiles': bad,
                                 'first': first})
                if not ok:
                    continue  # client may reconnect and retry with other iova
                rounds_done += 1
                if rounds_done >= args.rounds:
                    print('server: ALL %d ROUNDS PASSED — GPU memory content '
                          'byte-exact after RDMA writes' % args.rounds, flush=True)
                    conn.close()
                    srv.close()
                    sys.stdout.flush()
                    os._exit(0)  # avoid pyverbs GC teardown segfault
            elif msg['cmd'] == 'quit':
                break
        conn.close()
        srv.close()
    else:
        sock = socket.create_connection((args.ip, args.tcp_port), timeout=15)
        ep.reg_mr(gpu, IBV_ACCESS_LOCAL_WRITE)
        # pyverbs DmaBufMR registers with iova = offset (confirmed in
        # rdma-core pyverbs/mr.pyx: ibv_reg_dmabuf_mr(pd, offset, length,
        # offset, fd, access)), so MR-relative addressing is used on both
        # sides: [offset, offset+size).
        candidates = [gpu.offset, gpu.va]
        chosen = None
        for ci, cand in enumerate(candidates):
            total_bytes = 0
            total_time = 0.0
            ep.close()
            ep = RdmaEp(args.dev, args.gid_index)
            ep.reg_mr(gpu, IBV_ACCESS_LOCAL_WRITE)
            send_json(sock, {'cmd': 'hello', 'remote': ep.info()})
            ack = recv_json(sock)
            ep.connect(ack)
            print('client: QP established, trying iova candidate %d '
                  '(remote_addr=0x%x)' % (ci, cand), flush=True)
            ok = True
            write_err = False
            for rnd in range(args.rounds):
                magic = (0x5A + rnd * 37) & 0xFF
                gpu.fill(make_tile(magic))
                t0 = time.time()
                for off in range(0, gpu.size, CHUNK):
                    st, err = ep.rdma_write(cand + off, ack['rkey'],
                                            min(CHUNK, gpu.size - off),
                                            gpu.offset + off)
                    if st != 'OK':
                        print('client: round %d write FAILED at off=%d: %s/%s'
                              % (rnd, off, st, err), flush=True)
                        ok = False
                        write_err = True
                        break
                if not ok:
                    break
                dt = time.time() - t0
                total_bytes += gpu.size
                total_time += dt
                print('client: round %d magic=0x%02x wrote %d MiB in %.3fs '
                      '(%.2f Gb/s)' % (rnd, magic, gpu.size >> 20, dt,
                                       gpu.size * 8 / dt / 1e9), flush=True)
                send_json(sock, {'cmd': 'verify', 'round': rnd, 'magic': magic})
                vr = recv_json(sock)
                if not vr['ok']:
                    print('client: server verification FAILED round %d: %s'
                          % (rnd, vr), flush=True)
                    if not write_err:
                        # writes completed cleanly but content is wrong:
                        # genuine corruption, do not mask it with a retry
                        print('client: VERDICT: FAIL — data corruption '
                              'detected', flush=True)
                        send_json(sock, {'cmd': 'quit'})
                        sock.close()
                        sys.exit(3)
                    ok = False
                    break
            if ok:
                chosen = cand
                break
            print('client: iova candidate %d failed, reconnecting' % ci, flush=True)
        if chosen is None:
            print('client: NO iova candidate worked — test FAILED', flush=True)
            send_json(sock, {'cmd': 'quit'})
            sock.close()
            sys.exit(2)
        print('client: working iova remote_addr=0x%x' % chosen, flush=True)
        print('client: VERDICT: PASS — %d rounds, %d MiB total, %.2f Gb/s average'
              % (args.rounds, total_bytes >> 20,
                 total_bytes * 8 / total_time / 1e9 if total_time else 0),
              flush=True)
        try:
            send_json(sock, {'cmd': 'quit'})
            sock.close()
        except OSError:
            pass
        sys.stdout.flush()
        os._exit(0)  # avoid pyverbs GC teardown segfault


if __name__ == '__main__':
    main()
