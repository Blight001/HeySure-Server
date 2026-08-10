from api.integrations.media_source import MediaSource, infer_media_kind
from connector_runtime.bots.feishu.service import _feishu_file_type


def _source(name, mime):
    return MediaSource(path=name, filename=name, mime_type=mime)


def test_media_kind_supports_audio_and_generic_files():
    assert infer_media_kind(_source("voice.mp3", "audio/mpeg")) == "audio"
    assert infer_media_kind(_source("report.pdf", "application/pdf")) == "file"
    assert infer_media_kind(_source("archive.zip", "application/zip")) == "file"


def test_feishu_file_upload_type_mapping():
    assert _feishu_file_type(_source("report.pdf", "application/pdf")) == "pdf"
    assert _feishu_file_type(_source("sheet.xlsx", "application/vnd.ms-excel")) == "xls"
    assert _feishu_file_type(_source("archive.zip", "application/zip")) == "stream"
