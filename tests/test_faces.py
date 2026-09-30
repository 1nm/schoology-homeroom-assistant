"""Learning a child from a few class photos, matching, and remembering the verdicts --
all with a stubbed detector, so no model is needed."""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import faces
from faces import frame_around
import main
import slack_notify
from materials import Change, SyncReport


def unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=512).astype("float32")
    return v / np.linalg.norm(v)


CHILD, OTHER, THIRD = unit(1), unit(2), unit(3)


def jitter(v: np.ndarray, seed: int, amount: float = 0.15) -> np.ndarray:
    w = v + amount * unit(seed)
    return (w / np.linalg.norm(w)).astype("float32")


@pytest.fixture
def archive(tmp_path, monkeypatch):
    album = tmp_path / "materials" / "Photos" / "Week 4 (September)"
    album.mkdir(parents=True)
    # photo -> faces in it. The child is in 1, 2, 3 and 5; never in 4.
    scene = {
        "IMG_1.jpg": [jitter(CHILD, 11), OTHER],
        "IMG_2.jpg": [THIRD, jitter(CHILD, 12)],
        "IMG_3.jpg": [jitter(CHILD, 13)],
        "IMG_4.jpg": [OTHER, THIRD],
        "IMG_5.jpg": [jitter(CHILD, 15), jitter(OTHER, 16)],
        "IMG_6.jpg": [],
    }
    for name in scene:
        Image.new("RGB", (40, 30), "grey").save(album / name)

    def fake_detect(self, path):
        return [faces.Face(box=[0, 0, 10, 10], score=0.9, embedding=e) for e in scene[path.name]]

    monkeypatch.setattr(faces.FaceIndex, "_detect", fake_detect)
    return faces.FaceIndex(tmp_path)


def test_learn_picks_the_face_that_recurs(archive):
    report = archive.learn("kid", faces._resolve_marks(archive, ["Week 4:1,2,3"]))
    assert report["references"] == 3 and report["unsupported"] == []
    refs = np.array(archive.people()["kid"]["embeddings"])
    assert all(float(r @ CHILD) > 0.9 for r in refs), "every reference must be the child"


def test_learn_reports_photos_where_the_child_was_not_found(archive):
    report = archive.learn("kid", faces._resolve_marks(archive, ["Week 4:1,2,4"]))
    assert report["unsupported"] == ["materials/Photos/Week 4 (September)/IMG_4.jpg"]


def test_match_finds_every_photo_of_the_child_and_records_the_rest(archive):
    archive.learn("kid", faces._resolve_marks(archive, ["Week 4:1,2"]))
    found = archive.match("kid", threshold=0.6)

    assert [Path(m.path).name for m in found] == ["IMG_1.jpg", "IMG_2.jpg", "IMG_3.jpg", "IMG_5.jpg"]
    recorded = archive.matches()["kid"]
    assert len(recorded) == 6, "photos without the child are recorded too, so they are never re-judged"
    assert recorded["materials/Photos/Week 4 (September)/IMG_6.jpg"]["score"] == 0.0
    assert [Path(m.path).name for m in archive.photos_of("kid", 0.6)] == \
        [Path(m.path).name for m in found]


def test_scan_is_incremental(archive, monkeypatch):
    assert archive.scan() == 6
    assert archive.scan() == 0
    calls = []
    monkeypatch.setattr(faces.FaceIndex, "_detect", lambda self, p: calls.append(p) or [])
    (archive.photos_dir / "Week 4 (September)" / "IMG_7.jpg").write_bytes(b"")
    assert archive.scan() == 1 and len(calls) == 1


def test_contact_sheet_boxes_the_face(archive, tmp_path):
    archive.learn("kid", faces._resolve_marks(archive, ["Week 4:1,2"]))
    found = archive.match("kid", threshold=0.6)
    out = faces.contact_sheet(tmp_path, found, tmp_path / "sheet.jpg")
    with Image.open(out) as sheet:
        assert sheet.size == (len(found) * faces.THUMB, faces.THUMB), "no blank cells"


def test_sync_posts_a_sheet_of_new_photos_of_each_person(archive, settings, monkeypatch):
    import dataclasses
    settings = dataclasses.replace(settings, data_dir=archive.data_dir, face_threshold=0.6)
    archive.learn("kid", faces._resolve_marks(archive, ["Week 4:1,2"]))
    uploads = []
    monkeypatch.setattr(slack_notify, "notify_photos",
                        lambda s, person, count, albums, files: uploads.append((person, count, albums, files)))
    album = archive.photos_dir / "Week 4 (September)"
    report = SyncReport(changes=[
        Change("new", "Week 4", "IMG_3.jpg", "IMG_3.jpg", path=album / "IMG_3.jpg"),
        Change("new", "Week 4", "IMG_4.jpg", "IMG_4.jpg", path=album / "IMG_4.jpg"),
        Change("new", "Week 4", "clip.mov", "clip.mov", path=album / "clip.mov"),
    ])

    hits = main.find_people(settings, report)

    assert hits == {"kid": 1}
    assert len(uploads) == 1
    person, count, albums, files = uploads[0]
    assert (person, count, albums) == ("kid", 1, ["Week 4 (September)"])
    assert len(files) == 1 and files[0].exists(), "the photo goes out as itself, not a sheet"
    with Image.open(files[0]) as photo:
        assert photo.size == (40, 30)


def test_a_big_batch_goes_out_in_several_messages(settings, monkeypatch, tmp_path):
    import dataclasses
    settings = dataclasses.replace(settings, slack_bot_token="xoxb-t", slack_channel="C1",
                                   slack_mentions=["U1"])
    files = []
    for i in range(23):
        files.append(tmp_path / f"{i}.jpg")
        files[-1].write_bytes(b"x")
    messages = []
    monkeypatch.setattr(slack_notify, "upload_files",
                        lambda s, batch, comment: messages.append((len(batch), comment)))
    slack_notify.notify_photos(settings, "kid", 23, ["Week 4"], files)
    assert [n for n, _ in messages] == [10, 10, 3]
    assert messages[0][1].startswith("<@U1>") and "23 new photos" in messages[0][1]
    assert messages[1][1] == "(11–20 / 23)" and messages[2][1] == "(21–23 / 23)"


def test_upload_walks_the_three_steps(settings, monkeypatch, tmp_path):
    import dataclasses
    settings = dataclasses.replace(settings, slack_bot_token="xoxb-t", slack_channel="C1",
                                   slack_mentions=["U1"])
    sheet = tmp_path / "s.jpg"
    sheet.write_bytes(b"jpeg")
    calls = []

    class R:
        def __init__(self, payload):
            self.payload = payload
        def json(self):
            return self.payload
        def raise_for_status(self):
            pass

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("getUploadURLExternal"):
            return R({"ok": True, "upload_url": "https://up.example/x", "file_id": "F1"})
        return R({"ok": True})

    monkeypatch.setattr(slack_notify.requests, "post", post)
    slack_notify.notify_photos(settings, "kid", 2, ["Week 4"], [sheet])

    urls = [u for u, _ in calls]
    assert urls == ["https://slack.com/api/files.getUploadURLExternal", "https://up.example/x",
                    "https://slack.com/api/files.completeUploadExternal"]
    assert calls[1][1]["data"] == b"jpeg"
    done = calls[2][1]["json"]
    assert done["channel_id"] == "C1" and done["files"] == [{"id": "F1", "title": "Kid — Week 4"}]
    assert done["initial_comment"].startswith("<@U1>") and "2 new photos of Kid" in done["initial_comment"]


def test_review_adds_references_and_counter_examples(archive):
    archive.learn("kid", faces._resolve_marks(archive, ["Week 4:1,2"]))
    archive.match("kid", threshold=0.6)
    album = "materials/Photos/Week 4 (September)/"
    # A parent says IMG_5 is the child too, and IMG_4 (OTHER + THIRD) is not.
    report = archive.review("kid", accepted=[album + "IMG_5.jpg"], rejected=[album + "IMG_4.jpg"])

    assert report["added"] == 1 and report["rejected"] == 1
    people = archive.people()["kid"]
    assert len(people["references"]) == 3 and len(people["rejected"]) == 1
    # The rejected face is now a counter-example: a photo of only that face scores 0.
    counter = np.array([people["rejected"][0]["embedding"]], dtype="float32")
    assert archive._score(people, counter)[0] == 0.0
    # The child still matches, and every recorded verdict was recomputed.
    assert [Path(m.path).name for m in archive.photos_of("kid", 0.6)] == \
        ["IMG_1.jpg", "IMG_2.jpg", "IMG_3.jpg", "IMG_5.jpg"]


def test_review_skips_a_reference_that_barely_resembles_the_child(archive):
    archive.learn("kid", faces._resolve_marks(archive, ["Week 4:1,2"]))
    archive.match("kid", threshold=0.6)
    report = archive.review("kid", accepted=["materials/Photos/Week 4 (September)/IMG_4.jpg"],
                            rejected=[])
    assert report["added"] == 0 and report["skipped_low_quality"] == 1


def test_a_child_in_the_background_is_recorded_but_not_reported(archive, monkeypatch):
    archive.learn("kid", faces._resolve_marks(archive, ["Week 4:1,2"]))
    album = "materials/Photos/Week 4 (September)/"
    # In IMG_5 the other face is three times taller: the child is background there.
    archive.scan()
    archive._load_cache()[album + "IMG_5.jpg"]["boxes"] = [[0, 0, 10, 10], [0, 0, 30, 30]]
    found = archive.match("kid", 0.6, min_prominence=0.5)
    assert [Path(m.path).name for m in found] == ["IMG_1.jpg", "IMG_2.jpg", "IMG_3.jpg"]
    recorded = archive.matches()["kid"][album + "IMG_5.jpg"]
    assert recorded["prominence"] == pytest.approx(1 / 3, abs=0.01)
    assert len(archive.photos_of("kid", 0.6, min_prominence=0.3)) == 4


def test_small_faces_are_recorded_but_not_reported(archive, monkeypatch):
    archive.learn("kid", faces._resolve_marks(archive, ["Week 4:1,2"]))
    # Fixture photos are 40x30 and every box is 10px tall: a 1/3 share.
    assert [Path(m.path).name for m in archive.match("kid", 0.6, min_face=0.5)] == []
    recorded = archive.matches()["kid"]["materials/Photos/Week 4 (September)/IMG_1.jpg"]
    assert recorded["face"] == pytest.approx(10 / 30, abs=0.01)
    assert len(archive.photos_of("kid", 0.6, min_face=0.3)) == 4
    monkeypatch.setenv("FACE_MIN_SIZE", "0.5")
    assert archive.photos_of("kid", 0.6) == []


def test_frame_around_centres_the_face_and_stays_inside_the_picture():
    image = Image.new("RGB", (400, 300), "grey")
    # A 20px face near the right edge: the window is 45% of the short edge (135px
    # tall, 180px wide), centred as far right as the picture allows.
    crop = frame_around(image, [370, 140, 390, 160])
    assert crop.size == (180, 135)
    # and a face dead centre gives a window dead centre
    assert frame_around(image, [190, 140, 210, 160]).size == (180, 135)


def test_render_reframes_only_a_background_child(archive):
    archive.learn("kid", faces._resolve_marks(archive, ["Week 4:1,2"]))
    album = "materials/Photos/Week 4 (September)/"
    archive.scan()
    archive._load_cache()[album + "IMG_5.jpg"]["boxes"] = [[0, 0, 3, 3], [0, 0, 30, 30]]
    by_name = {Path(m.path).name: m for m in archive.match("kid", 0.6)}
    assert by_name["IMG_5.jpg"].prominence < 0.5 < by_name["IMG_1.jpg"].prominence
    whole = faces.render(archive.data_dir, by_name["IMG_1.jpg"])
    framed = faces.render(archive.data_dir, by_name["IMG_5.jpg"])
    assert whole.size == (40, 30)
    assert framed.size[0] < 40 and framed.size[1] < 30
