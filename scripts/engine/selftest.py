r"""搬机自检：确认解码器、依赖、分析链路在新机器上真的跑得通。

    runtime\python.exe selftest.py      （或 run.cmd selftest）

不通过就别急着扫全库 —— 参数再准，工具链断了也是白跑。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CREATE_NO_WINDOW = 0x08000000


def main() -> int:
    ok = True
    print("=" * 56)
    print(" DupeScope 搬机自检")
    print("=" * 56)

    print(f"[1/7] Python  {sys.version.split()[0]}  ({sys.executable})")
    try:
        import numpy as _numpy
        print(f"      {'numpy':10s} {getattr(_numpy, '__version__', 'ok')}")
    except Exception as e:  # noqa: BLE001
        print(f"      {'numpy':10s} 缺失 -> {e}")
        ok = False
    try:
        import rapidfuzz as _rapidfuzz
        print(f"      {'rapidfuzz':10s} {getattr(_rapidfuzz, '__version__', 'ok')}")
    except Exception as e:  # noqa: BLE001
        print(f"      {'rapidfuzz':10s} 缺失 -> {e}")
        ok = False
    try:
        import mutagen as _mutagen
        print(f"      {'mutagen':10s} {getattr(_mutagen, 'version_string', 'ok')}")
    except Exception as e:  # noqa: BLE001
        print(f"      {'mutagen':10s} 缺失 -> {e}")
        ok = False
    try:
        import pypinyin as _pypinyin
        print(f"      {'pypinyin':10s} {getattr(_pypinyin, '__version__', 'ok')}")
    except Exception as e:  # noqa: BLE001
        print(f"      {'pypinyin':10s} 缺失 -> {e}")
        ok = False

    from dsc import ffmpeg_bin

    print("[2/7] 解码器")
    for name, exe, arg in (("ffmpeg", ffmpeg_bin.ffmpeg(), "-version"),
                           ("ffprobe", ffmpeg_bin.ffprobe(), "-version")):
        try:
            r = subprocess.run([exe, arg], capture_output=True, text=True, timeout=20,
                               creationflags=CREATE_NO_WINDOW)
            ver = (r.stdout or r.stderr).splitlines()[0][:60]
            print(f"      {name:8s} OK  {ver}")
        except Exception as e:  # noqa: BLE001
            print(f"      {name:8s} 失败 -> {e}")
            ok = False

    sp = ffmpeg_bin.spek()
    print(f"[3/7] Spek     {'找到: ' + sp if sp else '未安装（只影响「人工复核」按钮，不影响分析）'}")

    print("[4/7] 分析链路（真跑一遍，两个已知答案的样本）")
    if not ok:
        print("      跳过：上面的依赖没齐")
        return 1

    from dsc import analyze, probe

    tmp = Path(tempfile.mkdtemp(prefix="dsc_selftest_"))
    ff = ffmpeg_bin.ffmpeg()
    good = tmp / "good.flac"
    fake = tmp / "fake.flac"
    mp3 = tmp / "x.mp3"
    base = ["-f", "lavfi", "-i", "anoisesrc=d=6:c=pink:r=44100:a=0.5:seed=3",
            "-af", "aformat=channel_layouts=stereo"]
    try:
        subprocess.run([ff, "-y", "-v", "error", *base, "-c:a", "flac", "-sample_fmt", "s16",
                        str(good)], check=True, creationflags=CREATE_NO_WINDOW)
        subprocess.run([ff, "-y", "-v", "error", *base, "-c:a", "libmp3lame", "-b:a", "128k",
                        str(mp3)], check=True, creationflags=CREATE_NO_WINDOW)
        subprocess.run([ff, "-y", "-v", "error", "-i", str(mp3), "-c:a", "flac",
                        "-sample_fmt", "s16", str(fake)], check=True,
                       creationflags=CREATE_NO_WINDOW)
    except Exception as e:  # noqa: BLE001
        print(f"      造样本失败 -> {e}")
        return 1

    res = {}
    for tag, f in (("真无损", good), ("128k 转的", fake)):
        m = probe.probe(f)
        a = analyze.analyze_audio(str(f), m)
        res[tag] = a
        print(f"      {tag:9s} 截止 {a.get('cutoff_khz')} kHz  DR {a.get('dr_value')}  "
              f"判定 {a.get('verdict')}")

    c_good = res["真无损"].get("cutoff_khz") or 0
    c_fake = res["128k 转的"].get("cutoff_khz") or 0
    checks = [
        ("真无损频带满幅 (>20 kHz)", c_good > 20),
        ("假无损被抓出低通 (<18 kHz)", c_fake < 18),
        ("DR 算得出来", (res["真无损"].get("dr_value") or 0) > 0),
        ("LUFS 算得出来", res["真无损"].get("lufs_i") is not None),
    ]
    for name, passed in checks:
        print(f"      [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ---------- 5. 顺序修正链路 ----------
    print("[5/7] 顺序修正（拼音排序 + 播放列表 + 撤销）")
    try:
        from dsc import order as od

        odir = tmp / "同一首歌"
        odir.mkdir()
        names = ["恰似你的温柔+张学友", "恰似你的温柔+蔡琴", "恰似你的温柔+王菲"]
        for n in names:
            subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=1",
                            "-ac", "2", "-c:a", "pcm_s16le", str(odir / f"{n}.wav")],
                           check=True, creationflags=CREATE_NO_WINDOW)

        plan = od.build_plan(odir, sort_by="title")
        got = [f["artist"] for f in plan["files"]]
        want = ["蔡琴", "王菲", "张学友"]          # cai < wang < zhang
        r1 = od.apply_plan(odir, sort_by="title", actions={"rename": True, "playlist": True},
                           confirm=True)
        pl = Path(r1["playlists"][0]).read_text(encoding="utf-8-sig")
        lines = [ln for ln in pl.splitlines() if ln and not ln.startswith("#")]
        und = od.undo(r1["manifest"])
        left = sorted(p.name for p in odir.iterdir() if p.suffix.lower() == ".wav")

        order_checks = [
            ("拼音排序正确（蔡→王→张）", got == want),
            ("播放列表按同一顺序", [Path(x).stem.split(" - ")[-1] for x in lines] == want),
            ("撤销后文件名复原", left == sorted(f"{n}.wav" for n in names)),
            ("撤销清单可读", und.get("restored") == 3),
        ]
        for name, passed in order_checks:
            print(f"      [{'PASS' if passed else 'FAIL'}] {name}")
            ok = ok and passed
    except Exception as e:  # noqa: BLE001
        print(f"      [FAIL] 顺序修正链路异常 -> {e}")
        ok = False

    # ---------- 6. 位对齐链路 ----------
    print("[6/7] 位对齐（AccurateRip checksum + cue TOC + 查库比对）")
    try:
        import struct
        from dsc import accuracy as ac
        # 已知向量：全零帧 -> 0；单帧 0x00010001 -> v1=v2=0x10001
        zc = ac.checksum_from_pcm(bytes(4 * 5000), False, False)
        oc = ac.checksum_from_pcm(struct.pack("<I", 0x00010001) + bytes(4 * 4999), False, False)
        ok_vec = (zc == (0, 0) and oc == (0x10001, 0x10001))

        adir = tmp / "cue_album"
        adir.mkdir()
        tracks = []
        for i in (1, 2):
            w = adir / f"t{i}.wav"
            subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi",
                            "-i", f"sine=frequency={300 + i * 100}:duration=5",
                            "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(w)],
                           check=True, creationflags=CREATE_NO_WINDOW)
            tracks.append(w)
        cue = adir / "album.cue"
        cue.write_text('TITLE "T"\nREM CDDB 1234abcd\n'
                       'FILE "t1.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
                       'FILE "t2.wav" WAVE\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n'
                       'REM LEAD-OUT 00:10:00\n', encoding="utf-8")
        cs = ac.parse_cue(cue)
        id1, id2 = ac.disc_ids(cs.tracks, cs.leadout_frame)
        c1 = ac.checksum_from_pcm(ac.decode_track_pcm(tracks[0]), True, False)
        c2 = ac.checksum_from_pcm(ac.decode_track_pcm(tracks[1]), False, True)
        hdr = struct.pack("<BIII", 2, id1, id2, 0x1234abcd)
        good_bin = hdr + struct.pack("<BII", 5, *c1) + struct.pack("<BII", 5, *c2)
        bad_bin = hdr + struct.pack("<BII", 5, c1[0] ^ 0xFF, c1[1]) + struct.pack("<BII", 5, *c2)
        r_ok = ac.verify_cue(cue, fetcher=lambda u: good_bin)
        r_bad = ac.verify_cue(cue, fetcher=lambda u: bad_bin)
        align_checks = [
            ("checksum 已知向量正确", ok_vec),
            ("cue TOC 解析出 2 轨", len(cs.tracks) == 2),
            ("真·逐位一致 -> ok", r_ok["status"] == "ok"),
            ("篡改后 -> mismatch", r_bad["status"] == "mismatch"),
        ]
        for name, passed in align_checks:
            print(f"      [{'PASS' if passed else 'FAIL'}] {name}")
            ok = ok and passed
    except Exception as e:  # noqa: BLE001
        print(f"      [FAIL] 位对齐链路异常 -> {e}")
        ok = False

    # ---------- 7. 响度归一链路 ----------
    print("[7/7] 响度归一（rsgain 只读扫描，不改原文件）")
    try:
        from dsc import gain as gn
        gstate = gn.check()
        print(f"      rsgain {'OK ' + gstate.get('version','') if gstate['ok'] else '未安装（跳过实测）'}")
        if gstate["ok"]:
            gdir = tmp / "gain_lib"; gdir.mkdir()
            srcflac = tmp / "good.flac"
            subprocess.run([ff, "-y", "-v", "error", "-i", str(srcflac),
                            "-c:a", "pcm_s16le", str(gdir / "01.wav")],
                           check=True, creationflags=CREATE_NO_WINDOW)
            before = (gdir / "01.wav").stat().st_mtime_ns
            gp = gn.plan(str(gdir))
            after = (gdir / "01.wav").stat().st_mtime_ns
            gain_checks = [
                ("扫出增益建议", len(gp.get("items", [])) >= 1),
                ("扫描不写标签（writes_tags=False）", gp.get("writes_tags") is False),
                ("原文件未被改动", before == after),
            ]
            for name, passed in gain_checks:
                print(f"      [{'PASS' if passed else 'FAIL'}] {name}")
                ok = ok and passed
        else:
            print("      [SKIP] 装了 rsgain 后可实测（不影响其它功能）")
    except Exception as e:  # noqa: BLE001
        print(f"      [FAIL] 响度归一链路异常 -> {e}")
        ok = False

    print("-" * 56)
    print(" 结论：" + ("一切就绪，可以扫库了。" if ok else "有项目没通过，先看上面的 ✗。"))
    print("=" * 56)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
