#!/usr/bin/env python3
"""Jadwalkan video YouTube Shorts ke Buffer berdasarkan jadwal.csv.

Perintah:
  python scripts/post_to_buffer.py channels   # tampilkan ID channel Buffer
  python scripts/post_to_buffer.py schedule   # jadwalkan baris berstatus 'pending'

Environment variable:
  BUFFER_API_KEY      (wajib)  API key dari Buffer: Settings -> API
  BUFFER_CHANNEL_ID   (wajib untuk 'schedule')
  GITHUB_REPOSITORY   otomatis ada di GitHub Actions, contoh: namamu/shorts-bot
  BRANCH              default: main
  LOOKAHEAD_DAYS      default: 3  (jadwalkan post yang tayang dalam N hari ke depan)
  DRY_RUN=1           hanya tampilkan apa yang akan dikirim, tanpa memanggil Buffer
  DRAFT=1             simpan sebagai draft di Buffer (untuk uji coba)
  AI_GENERATED=true   tandai pengungkapan konten AI di YouTube (default: false)
  CSV_PATH            default: jadwal.csv

Hanya memakai pustaka standar Python, jadi tidak perlu pip install.
"""
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_URL = "https://api.buffer.com"
USER_AGENT = "youtube-shorts-bot/1.0"
WIB = timezone(timedelta(hours=7))  # Waktu Indonesia Barat (Padang, Jakarta)
CSV_PATH = os.environ.get("CSV_PATH", "jadwal.csv")
FIELDS = [
    "id", "tanggal", "jam", "file_video", "judul",
    "deskripsi", "kategori", "status", "post_id", "catatan",
]

CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}
"""


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "ya")


def gql(query, variables=None):
    api_key = os.environ.get("BUFFER_API_KEY")
    if not api_key:
        sys.exit("BUFFER_API_KEY belum diisi.")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} dari Buffer: {detail}")
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL error: {json.dumps(payload['errors'])[:500]}")
    return payload["data"]


def cmd_channels():
    data = gql("query { account { organizations { id name } } }")
    for org in data["account"]["organizations"]:
        print(f"Organisasi: {org['name']}  (id: {org['id']})")
        ch = gql('query { channels(input: { organizationId: "%s" }) { id name service } }' % org["id"])
        for c in ch["channels"]:
            print(f"  - {c['service']:<10} {c['name']:<30} channel id: {c['id']}")


def video_url(filename):
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        sys.exit("GITHUB_REPOSITORY belum diisi (format: user/repo).")
    branch = os.environ.get("BRANCH", "main")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/videos/{urllib.parse.quote(filename)}"


def url_ok(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def read_rows():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in FIELDS:
            r.setdefault(k, "")
    return rows


def write_rows(rows):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def due_datetime(row):
    local = datetime.strptime(f"{row['tanggal'].strip()} {row['jam'].strip()}", "%Y-%m-%d %H:%M")
    return local.replace(tzinfo=WIB).astimezone(timezone.utc)


def cmd_schedule():
    dry = env_flag("DRY_RUN")
    draft = env_flag("DRAFT")
    ai_generated = env_flag("AI_GENERATED")
    lookahead = int(os.environ.get("LOOKAHEAD_DAYS", "3"))
    channel_id = os.environ.get("BUFFER_CHANNEL_ID", "")
    if not channel_id and not dry:
        sys.exit("BUFFER_CHANNEL_ID belum diisi.")

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=lookahead)
    rows = read_rows()
    failures = 0
    scheduled = 0

    candidates = []
    for r in rows:
        if r["status"].strip().lower() != "pending":
            continue
        try:
            due = due_datetime(r)
        except ValueError:
            print(f"[{r['id']}] format tanggal/jam salah (pakai YYYY-MM-DD dan HH:MM). Dilewati.")
            r["status"], r["catatan"] = "error", "format tanggal/jam salah"
            failures += 1
            continue
        if due <= now + timedelta(minutes=10):
            print(f"[{r['id']}] jadwal sudah lewat ({r['tanggal']} {r['jam']} WIB). Ditandai 'terlewat'.")
            r["status"], r["catatan"] = "terlewat", "jadwal sudah lewat saat diproses"
            continue
        if due > horizon:
            continue
        candidates.append((due, r))

    candidates.sort(key=lambda x: x[0])
    if not candidates:
        print("Tidak ada video yang perlu dijadwalkan hari ini.")

    for due, r in candidates:
        title = r["judul"].strip()
        if len(title) > 100:
            print(f"[{r['id']}] judul >100 karakter, dipotong.")
            title = title[:100]
        url = video_url(r["file_video"].strip())
        payload = {
            "text": r["deskripsi"].strip()[:5000],
            "channelId": channel_id or "DRY_RUN_CHANNEL",
            "schedulingType": "automatic",
            "mode": "customScheduled",
            "dueAt": due.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "assets": [{"video": {"url": url}}],
            "metadata": {
                "youtube": {
                    "title": title,
                    "categoryId": (r["kategori"].strip() or "27"),
                    "madeForKids": False,
                    "isAiGenerated": ai_generated,
                }
            },
        }
        if draft:
            payload["saveToDraft"] = True

        if dry:
            print(f"[{r['id']}] DRY RUN -> {json.dumps(payload, ensure_ascii=False)}")
            continue

        ok, info = url_ok(url)
        if not ok:
            print(f"[{r['id']}] video belum bisa diakses di {url} ({info}). Dilewati.")
            r["catatan"] = f"url video tidak bisa diakses: {info}"
            failures += 1
            continue

        try:
            data = gql(CREATE_POST, {"input": payload})
        except RuntimeError as e:
            print(f"[{r['id']}] GAGAL: {e}")
            r["catatan"] = str(e)[:200]
            failures += 1
            continue

        result = data.get("createPost") or {}
        if result.get("post"):
            r["status"] = "draft" if draft else "scheduled"
            r["post_id"] = result["post"]["id"]
            r["catatan"] = ""
            scheduled += 1
            print(f"[{r['id']}] OK -> tayang {r['tanggal']} {r['jam']} WIB (post {r['post_id']})")
            write_rows(rows)  # simpan progres setiap sukses agar tidak terjadi duplikat
        else:
            msg = result.get("message", "respons tidak dikenal")
            print(f"[{r['id']}] DITOLAK Buffer: {msg}")
            r["catatan"] = msg[:200]
            failures += 1
            if "queue" in msg.lower():
                print("Antrean Buffer penuh. Berhenti; akan dicoba lagi besok.")
                break

    if not dry:
        write_rows(rows)
    print(f"Selesai. Berhasil: {scheduled}, gagal: {failures}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("channels", "schedule"):
        sys.exit(__doc__)
    {"channels": cmd_channels, "schedule": cmd_schedule}[sys.argv[1]]()
