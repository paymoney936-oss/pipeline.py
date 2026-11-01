import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from upload import upload

HERE = Path(__file__).parent
USED_FILE = HERE / "used.json"
UA = {"User-Agent": "shorts-pipeline/1.0 (personal project)"}
GEO = os.getenv("TREND_GEO", "US")
VOICE = os.getenv("TTS_VOICE", "en-US-AriaNeural")
PRIVACY = os.getenv("PRIVACY", "private")


def load_used():
    return json.loads(USED_FILE.read_text()) if USED_FILE.exists() else []


def save_used(used):
    USED_FILE.write_text(json.dumps(used[-500:], indent=2))


def get_trends():
    r = requests.get(
        f"https://trends.google.com/trending/rss?geo={GEO}", headers=UA, timeout=30
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    return [i.findtext("title") for i in root.iter("item") if i.findtext("title")]


def get_facts(topic):
    s = requests.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "query", "list": "search", "srsearch": topic,
                "format": "json", "srlimit": 1},
        headers=UA, timeout=30,
    ).json()
    hits = s.get("query", {}).get("search", [])
    if not hits:
        return None
    title = hits[0]["title"]
    d = requests.get(
        "https://en.wikipedia.org/api/rest_v1/page/summary/"
        + requests.utils.quote(title.replace(" ", "_")),
        headers=UA, timeout=30,
    ).json()
    extract = d.get("extract", "")
    return (title, extract) if len(extract) > 200 else None


def write_script(topic, facts):
    key = os.getenv("GEMINI_API_KEY")
    if key:
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        prompt = (
            f"Write a 110-word voiceover for a 45-second YouTube Short about "
            f"'{topic}', which is trending right now. Start with a strong hook "
            f"question. Use ONLY these facts, in your own words, no emojis, no "
            f"stage directions, plain text only:\n\n{facts}"
        )
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": key}, timeout=60,
                json={"contents": [{"parts": [{"text": prompt}]}]},
            )
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            print("Gemini failed, using fallback script:", e)
    sentences = re.split(r"(?<=[.!?])\s+", facts)
    out, words = [f"Why is {topic} trending right now?"], 8
    for s in sentences:
        words += len(s.split())
        out.append(s)
        if words > 100:
            break
    return " ".join(out)


def make_voice(text, path):
    import edge_tts

    async def run():
        words = []
        comm = edge_tts.Communicate(text, VOICE, boundary="WordBoundary")
        with open(path, "wb") as f:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    start = chunk["offset"] / 1e7
                    words.append((chunk["text"], start, start + chunk["duration"] / 1e7))
        return words

    return asyncio.run(run())


def write_srt(words, path, per_line=3):
    def ts(t):
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"

    lines = []
    for n, i in enumerate(range(0, len(words), per_line), 1):
        group = words[i:i + per_line]
        text = " ".join(w[0] for w in group).upper()
        lines.append(f"{n}\n{ts(group[0][1])} --> {ts(group[-1][2])}\n{text}\n")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def audio_seconds(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)]
    )
    return float(out.strip())


def search_videos(key, query):
    r = requests.get(
        "https://pixabay.com/api/videos/",
        params={"key": key, "q": query[:90], "per_page": 15, "safesearch": "true"},
        headers=UA, timeout=30,
    ).json()
    return r.get("hits", [])


def get_clips(query, needed_seconds, workdir):
    key = os.environ["PEXELS_API_KEY"]  # holds your Pixabay key
    videos = search_videos(key, query)
    if len(videos) < 10:
            videos = search_videos(key, query)
    if len(videos) < 10:
        videos += search_videos(key, "nature landscape") + search_videos(key, "city lights")

    clips, total = [], 0.0
    for i, v in enumerate(videos):
        vids = v["videos"]
        url = (vids.get("large") or {}).get("url") or vids["medium"]["url"]
        raw = workdir / f"raw{i}.mp4"
        raw.write_bytes(requests.get(url, headers=UA, timeout=120).content)
        seg = workdir / f"seg{i}.mp4"
        length = min(6, v.get("duration", 6))
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-t", str(length),
             "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920,fps=30",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             str(seg)],
            check=True,
        )
        clips.append(seg)
        total += length
        if total >= needed_seconds + 1:
            break
    if total < needed_seconds:
        raise RuntimeError("Not enough footage found")
    return clips


def assemble(clips, voice, out, workdir):
    lst = workdir / "list.txt"
    lst.write_text("".join(f"file '{c}'\n" for c in clips))
    style = "FontSize=12,Bold=1,Outline=2,Alignment=2,MarginV=60"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-i", str(voice), "-map", "0:v", "-map", "1:a",
         "-vf", f"subtitles=captions.srt:force_style='{style}'",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(out)],
        check=True, cwd=str(workdir),
    )


def main():
    used = load_used()
    pick = None
    for topic in get_trends():
        if topic in used:
            continue
        facts = get_facts(topic)
        if facts:
            pick = (topic, *facts)
            break
    if not pick:
        sys.exit("No new trending topic with a usable article. Try again later.")

    topic, wiki_title, extract = pick
    print("Topic:", topic, "| Article:", wiki_title)

    script = write_script(topic, extract)
    print("Script:\n", script, "\n")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        voice = tmp / "voice.mp3"
        words = make_voice(script, voice)
        write_srt(words, tmp / "captions.srt")
        secs = audio_seconds(voice)
        if secs > 58:
            sys.exit(f"Voiceover is {secs:.0f}s; too long for a Short.")
        clips = get_clips(topic, secs, tmp)
        final = HERE / "output.mp4"
        assemble(clips, voice, final, tmp)

    title = f"{topic}: what you need to know #Shorts"
    desc = (f"{script}\n\nSource: https://en.wikipedia.org/wiki/"
            f"{wiki_title.replace(' ', '_')}\nStock footage: Pixabay")
    upload(str(final), title, desc, f"{topic},trending,explained,shorts", PRIVACY)

    used.append(topic)
    save_used(used)


if __name__ == "__main__":
    main()
