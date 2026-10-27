#!/usr/bin/env python3
"""B041 模型推理回放面板：把一次真机 run 目录（events.jsonl + request_*_obs.npz）
变成一个自包含的 HTML 文件，展台机器只要有浏览器就能放，不需要 Python。

    python3 replay_panel.py <run_dir> [-o out.html] [--title 文案]

只读 run 目录；标准库即可运行，有 numpy+Pillow 时图像走 JPEG（更小更快），
否则用纯 Python 编 PNG（慢一些，但结果一样）。
"""
import argparse
import base64
import io
import json
import os
import statistics
import struct
import sys
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
CAMS = [('head_rgb', '头部相机'), ('left_wrist_rgb', '左腕相机'), ('right_wrist_rgb', '右腕相机')]
# full23 布局：client_bundle/full23_adapter.py PARTS + lower_body = base(3) + torso(4)
GROUPS = [
    ('right_arm', '右臂', list(range(7, 14)), ['j1', 'j2', 'j3', 'j4', 'j5', 'j6', 'j7']),
    ('left_arm', '左臂', list(range(0, 7)), ['j1', 'j2', 'j3', 'j4', 'j5', 'j6', 'j7']),
    ('torso', '躯干', [19, 20, 21, 22], ['j1', 'j2', 'j3', 'j4']),
    ('gripper', '夹爪', [14, 15], ['左爪', '右爪']),
    ('base', '底盘', [16, 17, 18], ['vx', 'vy', 'wz']),
]


# ---------- npz / npy 解析（标准库） ----------
def parse_npy(buf):
    if buf[:6] != b'\x93NUMPY':
        raise ValueError('not npy')
    major = buf[6]
    if major == 1:
        hlen = struct.unpack('<H', buf[8:10])[0]; off = 10
    else:
        hlen = struct.unpack('<I', buf[8:12])[0]; off = 12
    header = buf[off:off + hlen].decode('latin1')
    descr = header.split("'descr':")[1].split(',')[0].strip().strip("'\"")
    shape = header.split("'shape':")[1].split(')')[0].strip().lstrip('(')
    shape = tuple(int(s) for s in shape.split(',') if s.strip())
    return descr, shape, buf[off + hlen:]


def image_to_data_url(descr, shape, raw):
    """(3,224,224) uint8 CHW 或 (224,224,3) HWC -> data URL。"""
    if descr != '|u1':
        raise ValueError('image dtype ' + descr)
    try:
        import numpy as np
        from PIL import Image
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(shape)
        if shape[0] == 3:
            arr = arr.transpose(1, 2, 0)
        bio = io.BytesIO()
        Image.fromarray(arr).save(bio, format='JPEG', quality=86)
        return 'data:image/jpeg;base64,' + base64.b64encode(bio.getvalue()).decode()
    except ImportError:
        pass
    # 纯 Python PNG
    if shape[0] == 3:
        c, h, w = shape
        plane = h * w
        r, g, b = raw[:plane], raw[plane:2 * plane], raw[2 * plane:3 * plane]
        rows = []
        for y in range(h):
            row = bytearray(1 + 3 * w)
            s = y * w
            row[1::3] = r[s:s + w]; row[2::3] = g[s:s + w]; row[3::3] = b[s:s + w]
            rows.append(bytes(row))
    else:
        h, w, c = shape
        rows = [b'\x00' + raw[y * w * 3:(y + 1) * w * 3] for y in range(h)]
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b'')
    return 'data:image/png;base64,' + base64.b64encode(png).decode()


def load_obs(path):
    out = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            key = name[:-4]
            descr, shape, raw = parse_npy(z.read(name))
            if key.endswith('_rgb'):
                out[key] = image_to_data_url(descr, shape, raw)
            elif key == 'state':
                n = shape[0]
                out['state'] = [round(v, 4) for v in struct.unpack('<%dd' % n, raw[:8 * n])]
    return out


# ---------- events.jsonl ----------
def r4(x):
    return round(x, 4)


def load_run(run_dir):
    ev = [json.loads(l) for l in open(os.path.join(run_dir, 'events.jsonl'), encoding='utf-8') if l.strip()]
    t0 = ev[0]['monotonic'] if 'monotonic' in ev[0] else min(e['monotonic'] for e in ev if 'monotonic' in e)
    summary = {}
    for name in ('SUMMARY.json', 'TRIAL_SUMMARY.json'):
        p = os.path.join(run_dir, name)
        if os.path.exists(p):
            summary[name] = json.load(open(p, encoding='utf-8'))
    chunks, targets, events, console = [], [], [], []
    first_resp = None
    for e in ev:
        k = e['event']
        if k == 'raw_response':
            resp = e.get('response', {})
            first_resp = first_resp or resp
            timing = e.get('observation_meta', {}).get('timing', {})
            ages = timing.get('receipt_age_s', {})
            c = {
                'i': e['chunk'],
                't_cap': r4(e.get('capture_monotonic', e['monotonic']) - t0),
                't_resp': r4(e['monotonic'] - t0),
                'rtt': r4(e.get('rtt_s', 0.0)),
                'server': r4(resp.get('server_queue_and_compute_ms', 0.0) / 1000.0),
                'state': [r4(v) for v in e.get('request_state23', [])],
                'action': [[r4(v) for v in row] for row in resp.get('action23', [])],
                'ages_ms': {k2: round(v * 1000) for k2, v in ages.items()},
                'skew_ms': round(timing.get('source_skew_s', 0) * 1000),
            }
            p = os.path.join(run_dir, 'request_%04d_obs.npz' % e['chunk'])
            if os.path.exists(p):
                obs = load_obs(p)
                c['img'] = {cam: obs.get(cam) for cam, _ in CAMS}
            chunks.append(c)
        elif k == 'target':
            targets.append([r4(e['monotonic'] - t0), e['chunk'], e['step'],
                            [r4(v) for v in e['target23']], [r4(v) for v in e['feedback23']]])
        elif k in ('grasp_detected', 'release_detected', 'clamped', 'request_failed', 'reconnected'):
            events.append({'t': r4(e['monotonic'] - t0), 'kind': k, 'chunk': e.get('chunk'),
                           'step': e.get('step'), 'detail': e.get('max_amount') or e.get('reason') or e.get('feedback')})
        elif k == 'console':
            console.append([r4(e['monotonic'] - t0), e['text']])
    # 步长：同一段内相邻 target 的时间差中位数
    dts = [targets[i + 1][0] - targets[i][0] for i in range(len(targets) - 1) if targets[i + 1][1] == targets[i][1]]
    step_dt = statistics.median(dts) if dts else 1 / 15.0
    exec_steps = max((tg[2] for tg in targets), default=7) + 1
    s = summary.get('SUMMARY.json', {})
    ts = summary.get('TRIAL_SUMMARY.json', {})
    run = s.get('run', {})
    meta = {
        'run_dir': os.path.basename(os.path.normpath(run_dir)),
        'started_utc': s.get('started_utc', ''),
        'end_reason': s.get('end_reason', ''),
        'exit_status': s.get('exit_status'),
        'task': (first_resp or {}).get('task', ''),
        'model_id': (first_resp or {}).get('model_id', ''),
        'ckpt': ((first_resp or {}).get('checkpoint_sha256') or '')[:12],
        'mode': (first_resp or {}).get('mode', ''),
        'horizon': (first_resp or {}).get('horizon', 32),
        'fps': (first_resp or {}).get('fps', 15),
        'fm_steps': (first_resp or {}).get('fm_steps'),
        'exec_steps': exec_steps,
        'step_dt': r4(step_dt),
        'chunks': run.get('chunks', len(chunks)),
        'steps': run.get('executed_steps', len(targets)),
        'model_time_s': r4(run.get('model_time_s', 0.0)),
        'wall_s': r4(run.get('wall_s', 0.0)),
        'rtt_median_s': r4(ts.get('rtt_median_s', statistics.median([c['rtt'] for c in chunks]) if chunks else 0)),
        'clamps': ts.get('clamps', sum(1 for e in events if e['kind'] == 'clamped')),
        'gripper_events': ts.get('gripper_events', []),
        'limit_overrides': s.get('limit_overrides', {}),
        't_end': r4(max(e['monotonic'] for e in ev if 'monotonic' in e) - t0),
    }
    return {'meta': meta, 'chunks': chunks, 'targets': targets, 'events': events, 'console': console,
            'groups': [{'key': k, 'label': l, 'idx': idx, 'names': n} for k, l, idx, n in GROUPS],
            'cams': [{'key': k, 'label': l} for k, l in CAMS]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run_dir')
    ap.add_argument('-o', '--out', default=None)
    ap.add_argument('--title', default='B041 · 模型推理回放')
    ap.add_argument('--subtitle', default='R1 Pro · 地面取布放入橙桶 · 真机 239')
    a = ap.parse_args()
    data = load_run(a.run_dir)
    data['meta']['title'] = a.title
    data['meta']['subtitle'] = a.subtitle
    tpl = open(os.path.join(HERE, 'panel_template.html'), encoding='utf-8').read()
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    html = tpl.replace('/*__DATA__*/null', payload)
    out = a.out or os.path.join(HERE, 'replay_%s.html' % data['meta']['run_dir'])
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    m = data['meta']
    print('写出 %s（%.1f MB）：%d 段 / %d 步 / %d 张图，结束原因 %s' % (
        out, os.path.getsize(out) / 1e6, len(data['chunks']), len(data['targets']),
        sum(len([v for v in c.get('img', {}).values() if v]) for c in data['chunks']), m['end_reason']))


if __name__ == '__main__':
    main()
