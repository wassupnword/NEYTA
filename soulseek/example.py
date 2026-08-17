"""Copy-paste starting points for a program built on soulseek_api."""

from soulseek_api import SoulseekClient, SoulseekError


def simple_search(query):
    """Search and print the ten most promising results."""
    with SoulseekClient.from_env() as sk:
        files = sk.search(query, timeout=20, audio_only=True)
        for f in files[:10]:
            print(f)
        return files


def grab_one(query, formats=("flac", "mp3")):
    """Search, pick the best match, download it, wait for it to land."""
    with SoulseekClient.from_env() as sk:
        files = sk.search(query, timeout=20, audio_only=True, extensions=formats)
        best = sk.best_match(files, prefer_extensions=formats)
        if best is None:
            print(f"nothing found for {query!r}")
            return None

        print(f"downloading {best}")
        transfer = sk.download_and_wait(
            best,
            timeout=600,
            on_progress=lambda t: print(f"  {t.percent:5.1f}%  {t.state}"),
        )
        print(f"saved as {transfer.basename} (in slskd's downloads directory)")
        return transfer


def grab_many(queries):
    """Queue up a batch of tracks, then wait on all of them.

    Enqueueing first and waiting second lets the transfers run in parallel.
    """
    with SoulseekClient.from_env() as sk:
        picks = []
        for query in queries:
            files = sk.search(query, timeout=20, audio_only=True)
            best = sk.best_match(files, prefer_extensions=["flac", "mp3"])
            if best:
                picks.append(best)
            else:
                print(f"skipped (no results): {query}")

        transfers = sk.download_many(picks)
        done = []
        for transfer in transfers:
            try:
                done.append(sk.wait_for_download(transfer, timeout=900))
                print(f"ok   {transfer.basename}")
            except SoulseekError as exc:
                print(f"fail {transfer.basename}: {exc}")
        return done


def whole_folder(username, directory):
    """Download every file in one user's directory — i.e. a full album."""
    with SoulseekClient.from_env() as sk:
        listing = sk.browse_directory(username, directory)
        for item in listing.get("files", []):
            sk.download(
                username=username,
                filename=f"{directory}\\{item['filename']}",
                size=item.get("size", 0),
            )
        return sk.get_downloads(username)


if __name__ == "__main__":
    with SoulseekClient.from_env() as client:
        print("connected" if client.ping() else "slskd is not connected to Soulseek")
