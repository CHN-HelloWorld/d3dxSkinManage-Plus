class UpdateDownloadBoundary:
    def has_local_md5(self, md5):
        return None

    def get_task(self, file_id):
        return None

    def retain(self, file_id, md5, *, owner, path=None):
        pass

    def release(self, owner):
        pass

    def has_other_consumers(self, file_id, owners):
        return False

    def delete_archive_when_unused(self, path):
        from src.services.gamebanana.download_store import delete_if_download

        delete_if_download(path)
