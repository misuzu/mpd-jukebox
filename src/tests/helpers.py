from mpd_jukebox.model import Song, freeze_tags


def song(uri: str, duration: float = 200.0, **tags: str | list[str]) -> Song:
    """A Song with lower-case tag keyword arguments."""
    return Song(
        uri=uri,
        tags=freeze_tags(
            {
                k: tuple(v) if isinstance(v, list) else (v,)
                for k, v in tags.items()
            }
        ),
        duration=duration,
    )
