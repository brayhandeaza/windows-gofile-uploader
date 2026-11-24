import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import upload_local_gofile as uploader


class LocalUploadTests(unittest.TestCase):
    def test_recursive_scan_only_videos(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'nested').mkdir()
            movie = root / 'nested' / 'movie.MKV'
            movie.write_bytes(b'movie')
            (root / 'notes.txt').write_text('notes')
            self.assertEqual(uploader.videos(root), [movie])

    def test_existing_requires_name_size_and_checksum(self):
        rows = [{'name': 'a.mkv', 'size': 10, 'md5': 'abc'}]
        self.assertIsNotNone(uploader.existing_file(rows, 'a.mkv', 10, 'abc'))
        self.assertIsNone(uploader.existing_file(rows, 'a.mkv', 10, 'different'))

    def test_upload_streams_original_and_validates_response(self):
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / 'a.mkv'
            payload = b'original movie bytes'
            movie.write_bytes(payload)
            checksum = uploader.digest(movie)
            class Session:
                def post(self, url, data, **kwargs):
                    self.body = b''.join(data)
                    return Response()
            class Response:
                def raise_for_status(self): pass
                def json(self):
                    return {'status': 'ok', 'data': {'parentFolder': 'folder', 'size': len(payload), 'md5': checksum}}
            session = Session()
            with patch.object(uploader, 'GoFile') as client:
                client.return_value.session = session
                result = uploader.transfer(movie, 'folder', len(payload), checksum, lambda **values: None)
            self.assertEqual(result['status'], 'uploaded')
            self.assertIn(payload, session.body)
            self.assertEqual(movie.read_bytes(), payload)

    def test_check_prompts_for_folder_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'movie.mp4').write_bytes(b'data')
            with patch('sys.argv', ['local', '--dry-run']), patch('builtins.input', return_value=directory) as prompt, patch.object(uploader, 'GoFile') as client, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(uploader.main(), 0)
                prompt.assert_called_once()
                client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
