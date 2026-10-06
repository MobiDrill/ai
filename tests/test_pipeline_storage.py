from dataclasses import replace

import pytest

from app.config import Settings
from app.services.pipeline_storage import PipelineStorage


def test_stage_paths_are_confined(tmp_path):
    storage = PipelineStorage(tmp_path)
    storage.prepare()
    job = storage.create_job(".pptx", "original.pptx")
    assert storage.stage_directory(job.job_id, 2) == job.directory / "2"
    for index in (-1, "../../outside", True):
        with pytest.raises(ValueError):
            storage.stage_directory(job.job_id, index)
    with pytest.raises(ValueError):
        storage.stage_directory("../outside", 2)
    with pytest.raises(ValueError):
        storage.create_job("/../../outside", "file")


def test_temporary_cleanup_cannot_share_pipeline_storage(tmp_path):
    with pytest.raises(ValueError, match="separate"):
        replace(Settings(), work_root=tmp_path, storage_root=tmp_path / "files")
    with pytest.raises(ValueError, match="separate"):
        replace(Settings(), work_root=tmp_path / "runtime", storage_root=tmp_path)
