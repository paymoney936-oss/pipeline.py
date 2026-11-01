import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_service():
    cid = os.environ["YT_CLIENT_ID"].strip()
    csec = os.environ["YT_CLIENT_SECRET"].strip()
    rtok = os.environ["YT_REFRESH_TOKEN"].strip()
    print("DEBUG client id:", cid)
    print("DEBUG secret length:", len(csec),
          "| token length:", len(rtok), "| token starts:", rtok[:3])
    creds = Credentials(
        None,
        refresh_token=rtok,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cid,
        client_secret=csec,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload(path, title, description, tags, privacy="private"):
    youtube = get_service()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media
    )
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Uploaded {int(status.progress() * 100)}%")
    print(f"Done: https://youtube.com/watch?v={response['id']}")
