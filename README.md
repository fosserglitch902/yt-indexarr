# YouTube Indexer

Index and find YouTube videos matching a specific TV episode.

## Scripts

### `yt-episode-search.sh`

Search YouTube for a specific episode of a series and rank the results by how
likely each video is the actual full episode.

```sh
./yt-episode-search.sh -s "My Show" -S 1 -E 2 -t "The Cave"
```

Requires `yt-dlp` and `jq`.

See `./yt-episode-search.sh -h` for all options.
