from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .llm import GeneratedProfile


DOC_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]


@dataclass(frozen=True)
class StoredDoc:
    doc_id: str | None
    url: str | None


class GoogleDocsProfileClient:
    def __init__(self, *, service_account_file: Path | None, drive_folder_id: str | None):
        self.service_account_file = service_account_file
        self.drive_folder_id = drive_folder_id
        self._docs = None
        self._drive = None

    @property
    def enabled(self) -> bool:
        return self.service_account_file is not None and self.service_account_file.exists()

    def read_doc_text(self, doc_id: str | None) -> str:
        if not self.enabled or not doc_id:
            return ""

        document = self._docs_service().documents().get(documentId=doc_id).execute()
        return document_plain_text(document)

    def upsert_profile_doc(
        self,
        *,
        doc_id: str | None,
        username: str,
        profile: GeneratedProfile,
    ) -> StoredDoc:
        if not self.enabled:
            return StoredDoc(doc_id=doc_id, url=doc_url(doc_id))

        if not doc_id:
            doc_id = self._create_doc(username)

        document = self._docs_service().documents().get(documentId=doc_id).execute()
        end_index = document_end_index(document)
        text = format_profile_doc(username, profile)
        requests = []
        if end_index > 2:
            requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}})
        requests.append({"insertText": {"location": {"index": 1}, "text": text}})
        self._docs_service().documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests},
        ).execute()
        return StoredDoc(doc_id=doc_id, url=doc_url(doc_id))

    def _create_doc(self, username: str) -> str:
        title = f"Discord Profile - {username}"
        if self.drive_folder_id:
            file = (
                self._drive_service()
                .files()
                .create(
                    body={
                        "name": title,
                        "mimeType": "application/vnd.google-apps.document",
                        "parents": [self.drive_folder_id],
                    },
                    fields="id",
                )
                .execute()
            )
            return str(file["id"])

        document = self._docs_service().documents().create(body={"title": title}).execute()
        return str(document["documentId"])

    def _credentials(self):
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            str(self.service_account_file),
            scopes=DOC_SCOPES,
        )

    def _docs_service(self):
        if self._docs is None:
            from googleapiclient.discovery import build

            self._docs = build("docs", "v1", credentials=self._credentials())
        return self._docs

    def _drive_service(self):
        if self._drive is None:
            from googleapiclient.discovery import build

            self._drive = build("drive", "v3", credentials=self._credentials())
        return self._drive


def doc_url(doc_id: str | None) -> str | None:
    if not doc_id:
        return None
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def format_profile_doc(username: str, profile: GeneratedProfile) -> str:
    return (
        f"{username}\n\n"
        f"Summary\n{profile.summary}\n\n"
        f"Interests\n{profile.interests}\n\n"
        f"Communication Style\n{profile.communication_style}\n\n"
        f"Known Facts\n{profile.known_facts}\n\n"
        f"Recent Updates\n{profile.recent_updates}\n"
    )


def document_plain_text(document: dict) -> str:
    parts: list[str] = []
    for element in document.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for item in paragraph.get("elements", []):
            text_run = item.get("textRun")
            if text_run:
                parts.append(text_run.get("content", ""))
    return "".join(parts).strip()


def document_end_index(document: dict) -> int:
    content = document.get("body", {}).get("content", [])
    if not content:
        return 1
    return int(content[-1].get("endIndex", 1))
