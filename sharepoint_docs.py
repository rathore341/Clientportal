from urllib.parse import urlparse


class SharePointDocumentError(ValueError):
    pass


def get_client_document_link(client):
    document_link = str(client.sharepoint_folder_path or "").strip()
    if not document_link:
        raise SharePointDocumentError(
            "Document link is not mapped for this client yet. Ask admin to set the SharePoint document link."
        )

    parsed_link = urlparse(document_link)
    if parsed_link.scheme not in {"http", "https"} or not parsed_link.netloc:
        raise SharePointDocumentError(
            "Client document link must be a full SharePoint URL created for this client."
        )

    if "sharepoint.com" not in parsed_link.netloc.lower():
        raise SharePointDocumentError("Client document link must point to SharePoint.")

    return document_link
