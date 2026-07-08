"""
tests/test_notifications.py — Mixtape

Tests for notification creation logic.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed_rating_scenario(app):
    """A song shared by one user, to be rated by another (or by the sharer)."""
    with app.app_context():
        sharer = User(username="aaliya", email="aaliya@example.com")
        rater = User(username="kenji", email="kenji@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Golden Hour", artist="Solange K", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_a_friends_song_notifies_the_sharer(app, seed_rating_scenario):
    """
    Rating a friend's shared song should notify the person who shared it.

    Bug: rate_song() saved the rating but never called create_notification(),
    so the sharer's notification list stayed empty no matter who rated their song.
    """
    with app.app_context():
        sharer_id = seed_rating_scenario["sharer"].id
        rater_id = seed_rating_scenario["rater"].id
        song_id = seed_rating_scenario["song"].id

        rate_song(rater_id, song_id, 5)

        notifications = get_notifications(sharer_id)
        assert len(notifications) == 1  # Bug caused this to be 0
        assert notifications[0]["type"] == "song_rated"
        assert "kenji" in notifications[0]["body"]
        assert "Golden Hour" in notifications[0]["body"]


def test_rating_your_own_song_does_not_notify_yourself(app, seed_rating_scenario):
    """Rating your own shared song should not generate a self-notification."""
    with app.app_context():
        sharer_id = seed_rating_scenario["sharer"].id
        song_id = seed_rating_scenario["song"].id

        rate_song(sharer_id, song_id, 4)

        assert get_notifications(sharer_id) == []
