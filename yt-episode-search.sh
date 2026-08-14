#!/usr/bin/env bash
set -euo pipefail


usage() {
  cat <<'EOF'
Usage:
  ./yt-episode-search.sh -s "Series Name" -S 1 -E 2 -t "Episode Title" [options]
  ./yt-episode-search.sh -s "Series Name" -S 3 -P [options]


Options:
  -s SERIES      Series name (required)
  -S SEASON      Season number (required)
  -E EPISODE     Episode number (required unless -P)
  -P             Playlist/season mode: find playlists for the whole season
                 (no episode number; emits playlist season-packs)
  -t TITLE       Episode title (optional but strongly recommended;
                 repeatable for localized/alternate titles)
  -n MAX         Max final results to output (default: 20)
  -p PER_QUERY   Number of results per YouTube query (default: 5)
  -d DELAY       Ignored (retained for compatibility; queries now run
                 concurrently per SEARCH_PARALLEL)
  -j             Output JSON lines instead of a table
  -b             Print best candidate URL only
  -D             Download best candidate with yt-dlp
  -o DIR         Download directory, used with -D (default: downloads)
  -h             Show help


Environment:
  MIN_DURATION   Minimum video length in seconds (default: 300)
  EXPECTED_DURATION  Expected episode runtime in seconds; when set, results are
                 filtered to this +/- EP_DURATION_BUFFER at the search stage so
                 multi-episode compilations are excluded (default: empty)
  EP_DURATION_BUFFER  +/- seconds around EXPECTED_DURATION (default: 60)
  RESOLVE_TOP    Number of top candidates to re-probe for resolution
                 metadata (default: 5, 0 disables the pass)
  SEARCH_PARALLEL  YouTube queries run concurrently (default: 3; set 1 to
                 force sequential execution)


Examples:
  ./yt-episode-search.sh -s "My Show" -S 1 -E 2 -t "The Cave"
  ./yt-episode-search.sh -s "My Show" -S 1 -E 2 -t "The Cave" -j
  ./yt-episode-search.sh -s "My Show" -S 1 -E 2 -t "The Cave" -b
  ./yt-episode-search.sh -s "My Show" -S 1 -E 2 -t "The Cave" -D
  ./yt-episode-search.sh -s "My Show" -S 3 -P -j
EOF
}


require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing dependency: $1" >&2
    exit 1
  }
}


SERIES=""
SEASON=""
EPISODE=""
PLAYLIST_MODE=false
declare -a EP_TITLES=()
MAX_RESULTS=20
PER_QUERY=5
DELAY=1
MIN_DURATION=${MIN_DURATION:-300}
RESOLVE_TOP=${RESOLVE_TOP:-5}
EXPECTED_DURATION=${EXPECTED_DURATION:-}
EP_DURATION_BUFFER=${EP_DURATION_BUFFER:-60}
# How many YouTube queries to run concurrently (single-episode and season
# search).  Default 3 to stay gentle on rate limits; set 1 for fully serial,
# or higher (up to the query count) for the fastest response.
SEARCH_PARALLEL=${SEARCH_PARALLEL:-3}
PLAYER_CLIENT=${PLAYER_CLIENT:-tv_embedded,android_vr,web,tv_simply,android}
export PLAYER_CLIENT
# Optional bgutil PO token provider URL (e.g. http://pot:4416). Empty disables.
POT_PROVIDER=${POT_PROVIDER:-}
export POT_PROVIDER
JSON_OUT=false
BEST_ONLY=false
DOWNLOAD=false
DOWNLOAD_DIR="downloads"


while getopts ":s:S:E:t:n:p:d:o:DPbjh" opt; do
  case "$opt" in
    s) SERIES="$OPTARG" ;;
    S) SEASON="$OPTARG" ;;
    E) EPISODE="$OPTARG" ;;
    P) PLAYLIST_MODE=true ;;
    t) EP_TITLES+=("$OPTARG") ;;
    n) MAX_RESULTS="$OPTARG" ;;
    p) PER_QUERY="$OPTARG" ;;
    d) DELAY="$OPTARG" ;;
    o) DOWNLOAD_DIR="$OPTARG" ;;
    D) DOWNLOAD=true ;;
    b) BEST_ONLY=true ;;
    j) JSON_OUT=true ;;
    h) usage; exit 0 ;;
    \?) echo "Unknown option: -$OPTARG" >&2; usage; exit 1 ;;
    :) echo "Option -$OPTARG requires an argument" >&2; usage; exit 1 ;;
  esac
done


if [[ -z "$SERIES" ]]; then
  usage
  exit 1
fi


if ! [[ "$SEASON" =~ ^[0-9]+$ ]]; then
  echo "ERROR: season must be numeric" >&2
  exit 1
fi


if ! $PLAYLIST_MODE && ! [[ "$EPISODE" =~ ^[0-9]+$ ]]; then
  echo "ERROR: episode must be numeric" >&2
  exit 1
fi


if ! [[ "$MAX_RESULTS" =~ ^[0-9]+$ ]]; then
  MAX_RESULTS=20
fi


if ! [[ "$PER_QUERY" =~ ^[0-9]+$ ]]; then
  PER_QUERY=5
fi


if ! [[ "$DELAY" =~ ^[0-9]+$ ]]; then
  DELAY=1
fi


if ! [[ "$MIN_DURATION" =~ ^[0-9]+$ ]]; then
  MIN_DURATION=300
fi


if [[ -n "$EXPECTED_DURATION" ]] && ! [[ "$EXPECTED_DURATION" =~ ^[0-9]+$ ]]; then
  EXPECTED_DURATION=
fi


if ! [[ "$EP_DURATION_BUFFER" =~ ^[0-9]+$ ]]; then
  EP_DURATION_BUFFER=60
fi


if ! [[ "$SEARCH_PARALLEL" =~ ^[0-9]+$ ]] || (( SEARCH_PARALLEL < 1 )); then
  SEARCH_PARALLEL=3
fi


# Normalize leading zeros: 01 -> 1
SEASON=$((10#$SEASON))
if ! $PLAYLIST_MODE; then
  EPISODE=$((10#$EPISODE))
fi


# Duration window.  When EXPECTED_DURATION (seconds) is known, filter results
# to the episode's runtime +/- EP_DURATION_BUFFER so multi-episode
# compilations are excluded at the search stage (yt-dlp fetches more to still
# fill the per-query count).  Otherwise fall back to the MIN_DURATION floor.
if [[ -n "$EXPECTED_DURATION" ]]; then
  DUR_MIN=$(( EXPECTED_DURATION - EP_DURATION_BUFFER ))
  DUR_MAX=$(( EXPECTED_DURATION + EP_DURATION_BUFFER ))
  if (( DUR_MIN < MIN_DURATION )); then
    DUR_MIN=$MIN_DURATION
  fi
fi


require_cmd yt-dlp
require_cmd jq


# ---- Playlist/season mode ----------------------------------------------
# A full-season interactive search has no episode number; instead we find
# playlists covering the whole season.  yt-dlp has no ytsearch:playlists
# prefix, but its YoutubeSearchURLIE honours YouTube's "sp" search filter;
# sp=EgIQAw%3D%3D restricts results to playlists only (verified live).
if $PLAYLIST_MODE; then
  P_WORK="$(mktemp -d)"
  trap 'rm -rf "$P_WORK"' EXIT

  p_queries=(
    "$SERIES season $SEASON"
    "$SERIES s$SEASON"
    "$SERIES full episodes"
    "$SERIES episodes"
  )

  # jq filter shared by every playlist query (avoids quoting it per subshell).
  cat > "$P_WORK/filter.jq" <<'JQ'
select(.id != null) |
select((.url // "") | startswith("https://www.youtube.com/playlist")) |
{
  query: $query,
  id: .id,
  title: (.title // ""),
  url: .url,
  playlist_count: (.playlist_count // 0),
  channel: (.channel // .uploader // "")
}
JQ

  # Seed per-query inputs, then run queries concurrently (SEARCH_PARALLEL).
  for i in "${!p_queries[@]}"; do
    printf '%s\n' "${p_queries[$i]}" > "$P_WORK/q.$i.txt"
  done
  seq 0 $(( ${#p_queries[@]} - 1 )) | xargs -P "$SEARCH_PARALLEL" -I{} bash -c '
    i="$1"; P_WORK="$2"
    pq="$(cat "$P_WORK/q.$i.txt")"
    echo "Playlist query: $pq" >&2
    enc="$(printf "%s" "$pq" | jq -sRr @uri)"
    # --playlist-end caps the full results-page scrape to the top hits (we
    # only keep up to MAX_RESULTS anyway); without it yt-dlp drains the whole
    # page (~460 entries, ~16s per query).
    yt-dlp \
      --ignore-config \
      --flat-playlist \
      --dump-json \
      --no-warnings \
      --retries 3 \
      --playlist-end 30 \
      "https://www.youtube.com/results?search_query=${enc}&sp=EgIQAw%3D%3D" 2>>"$P_WORK/err.log" |
      jq -c --arg query "$pq" -f "$P_WORK/filter.jq" > "$P_WORK/raw.$i.jsonl" || true
  ' _ {} "$P_WORK"

  # Concatenate in original query order so dedup keeps the same first-seen.
  : > "$P_WORK/raw.jsonl"
  for i in "${!p_queries[@]}"; do
    cat "$P_WORK/raw.$i.jsonl" >> "$P_WORK/raw.jsonl" 2>/dev/null || true
  done


  if [[ ! -s "$P_WORK/raw.jsonl" ]]; then
    echo "ERROR: no playlists found." >&2
    if [[ -s "$P_WORK/err.log" ]]; then
      echo "yt-dlp errors:" >&2
      tail -n 20 "$P_WORK/err.log" >&2
    fi
    exit 1
  fi


  # Deduplicate by playlist id, keeping the best (first-seen) title.
  jq -c -s --argjson MAX "$MAX_RESULTS" '
    map(select(.id != null))
    | group_by(.id)
    | map({
        id: .[0].id,
        title: .[0].title,
        url: .[0].url,
        playlist_count: .[0].playlist_count,
        channel: .[0].channel,
        queries: ([.[].query] | unique)
      })
    | .[:$MAX][]
  ' "$P_WORK/raw.jsonl" > "$P_WORK/final.jsonl"

  # Optional quality pass: resolve the first video of each top playlist to
  # recover its resolution, view count and playlist size (flat search results
  # carry none of these; a full --dump-json with --playlist-items 1 adds them
  # in ~1-2s per playlist).  A season playlist's first entry is episode 1, so
  # its quality and popularity represent the pack.  Set RESOLVE_TOP=0 to skip.
  P_RESOLVED="$P_WORK/resolved.jsonl"
  if [[ "$RESOLVE_TOP" -gt 0 ]] && command -v xargs >/dev/null 2>&1; then
    head -n "$RESOLVE_TOP" "$P_WORK/final.jsonl" | jq -r '.url // empty' |
      xargs -P 4 -I{} sh -c \
        'yt-dlp --ignore-config --dump-json --no-warnings --skip-download --retries 3 --playlist-items 1 --extractor-args "youtube:player_client=$PLAYER_CLIENT" $(if [ -n "$POT_PROVIDER" ]; then printf -- "--extractor-args youtubepot-bgutilhttp:base_url=$POT_PROVIDER"; fi) "$1" 2>/dev/null | jq -c --arg url "$1" '"'"'{url: $url, height: (.height // 0), view_count: (.view_count // 0), playlist_count: (.playlist_count // 0)}'"'"' ' _ {} \
        > "$P_RESOLVED" 2>/dev/null || true
    jq -c -s --slurpfile meta "$P_RESOLVED" '
      def qlabel($h):
        if $h >= 2160 then "2160p"
        elif $h >= 1440 then "1440p"
        elif $h >= 1080 then "1080p"
        elif $h >= 720 then "720p"
        elif $h >= 480 then "480p"
        else "360p" end;
      ($meta | map({key: .url, value: .}) | from_entries) as $m |
      .[] |
      . + {
        resolution: ((if ($m[.url].height // 0) > 0 then qlabel($m[.url].height) else null end)),
        views: ($m[.url].view_count // 0),
        playlist_count: ((if (.playlist_count // 0) > 0 then .playlist_count else ($m[.url].playlist_count // 0) end))
      }
    ' "$P_WORK/final.jsonl" > "$P_WORK/final2.jsonl"
    mv "$P_WORK/final2.jsonl" "$P_WORK/final.jsonl"
  fi

  if $JSON_OUT; then
    cat "$P_WORK/final.jsonl"
  else
    jq -r '[.id, (.playlist_count // 0), (.channel // ""), .title, .url] | @tsv' \
      "$P_WORK/final.jsonl" |
      if command -v column >/dev/null 2>&1; then
        column -t -s $'\t'
      else
        cat
      fi
  fi
  exit 0
fi
# ---- end playlist/season mode ------------------------------------------


SE_TAG=$(printf "S%02dE%02d" "$SEASON" "$EPISODE")


# Build multiple search variants.
# YouTube search is noisy, so more query shapes usually help.
queries=(
  "$SERIES $SE_TAG"
  "$SERIES season $SEASON episode $EPISODE"
  "$SERIES episode $EPISODE"
)


# Query expansion uses the primary (first) title only, so the number of
# YouTube searches stays bounded; all titles still count for scoring below.
PRIMARY_TITLE="${EP_TITLES[0]:-}"
if [[ -n "$PRIMARY_TITLE" ]]; then
  queries+=(
    "$SERIES $SE_TAG $PRIMARY_TITLE"
    "$SERIES $PRIMARY_TITLE"
    "$SERIES episode $EPISODE $PRIMARY_TITLE"
  )
fi


WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT


RAW="$WORKDIR/raw.jsonl"
DEDUP="$WORKDIR/dedup.jsonl"
SCORED="$WORKDIR/scored.jsonl"
SORTED="$WORKDIR/sorted.jsonl"
RESOLVED="$WORKDIR/resolved.jsonl"
FINAL="$WORKDIR/final.jsonl"
ERRLOG="$WORKDIR/yt-dlp.err"


echo "Searching YouTube..." >&2


# jq filter shared by every query (avoids quoting it inside the subshells).
cat > "$WORKDIR/filter.jq" <<'JQ'
select(.id != null) |
select((.duration // 0) >= $min_duration) |
select(if $dur_max > 0 then (.duration // 0) <= $dur_max else true end) |
{
  query: $query,
  id: .id,
  title: (.title // ""),
  url: (.webpage_url // ("https://www.youtube.com/watch?v=" + .id)),
  duration: (.duration // 0),
  timestamp: (.timestamp // 0),
  channel: (.channel // .uploader // ""),
  views: (.view_count // 0)
}
JQ

# --flat-playlist makes search much faster because it does not resolve every
# video fully.  --dump-json prints one JSON object per result.
if [[ -n "$EXPECTED_DURATION" ]]; then
  QT_FILTER="duration >= ${DUR_MIN} and duration <= ${DUR_MAX}"
else
  QT_FILTER="duration >= ${MIN_DURATION}"
fi
export PER_QUERY QT_FILTER MIN_DURATION DUR_MIN DUR_MAX

# Seed per-query inputs, then run queries concurrently (SEARCH_PARALLEL).
for i in "${!queries[@]}"; do
  printf '%s\n' "${queries[$i]}" > "$WORKDIR/q.$i.txt"
done
seq 0 $(( ${#queries[@]} - 1 )) | xargs -P "$SEARCH_PARALLEL" -I{} bash -c '
  i="$1"; W="$2"; filter="$3"
  q="$(cat "$W/q.$i.txt")"
  echo "Query: $q" >&2
  yt-dlp \
    --ignore-config \
    --flat-playlist \
    --dump-json \
    --no-warnings \
    --retries 3 \
    --match-filter "$QT_FILTER" \
    "ytsearch${PER_QUERY}:${q}" 2>>"$W/yt.err" |
    jq -c \
      --arg query "$q" \
      --argjson min_duration "$MIN_DURATION" \
      --argjson dur_min "${DUR_MIN:-$MIN_DURATION}" \
      --argjson dur_max "${DUR_MAX:-0}" \
      -f "$filter" > "$W/raw.$i.jsonl" || true
' _ {} "$WORKDIR" "$WORKDIR/filter.jq"

# Concatenate in original query order so dedup keeps the same first-seen.
: > "$RAW"
for i in "${!queries[@]}"; do
  cat "$WORKDIR/raw.$i.jsonl" >> "$RAW" 2>/dev/null || true
done
cat "$WORKDIR/yt.err" >> "$ERRLOG" 2>/dev/null || true


if [[ ! -s "$RAW" ]]; then
  echo "ERROR: no results from any query." >&2
  if [[ -s "$ERRLOG" ]]; then
    echo "yt-dlp errors:" >&2
    tail -n 20 "$ERRLOG" >&2
  fi
  exit 1
fi


# Deduplicate by video id.
jq -s '
  map(select(.id != null))
  | group_by(.id)
  | map({
      id: .[0].id,
      title: .[0].title,
      url: .[0].url,
      duration: .[0].duration,
      timestamp: .[0].timestamp,
      channel: .[0].channel,
      views: .[0].views,
      queries: ([.[].query] | unique)
    })
  | .[]
' "$RAW" > "$DEDUP"


# Build episode token variants to look for in video titles.
# Examples for S01E02:
#   s01e02, s1e2, 1x02, episode 2, ep 2, #2, part 2, etc.
EP_TOKENS_JSON="$(
  jq -cn --argjson s "$SEASON" --argjson e "$EPISODE" '
    def pad2: tostring | if length < 2 then "0" + . else . end;


    ($s | tostring) as $sn |
    ($e | tostring) as $en |
    ($sn | pad2) as $sp |
    ($en | pad2) as $ep |


    [
      "s\($sp)e\($ep)",
      "s\($sn)e\($en)",
      "s\($sp)e\($en)",
      "s\($sn)e\($ep)",


      "s\($sp) e\($ep)",
      "s\($sn) e\($en)",


      "season \($sn) episode \($en)",
      "season \($sp) episode \($ep)",


      "season \($sn) ep \($en)",
      "season \($sp) ep \($ep)",


      "\($sn)x\($ep)",
      "\($sn)x\($en)",


      "episode \($en)",
      "episode \($ep)",
      "episode\($en)",
      "episode\($ep)",


      "ep \($en)",
      "ep \($ep)",
      "ep\($en)",
      "ep\($ep)",


      "ep.\($en)",
      "ep.\($ep)",


      "#\($en)",
      "#\($ep)",


      "part \($en)",
      "part \($ep)"
    ]
  '
)"

# All episode-title match targets as a JSON array (primary + localized).
EP_TITLES_JSON="$(
  printf '%s\0' "${EP_TITLES[@]}" \
    | jq -Rs 'split("\u0000") | map(select(length > 0))'
)"


# Score candidates.
#
# This is intentionally heuristic. It tries to answer:
# - Does the title contain the series name?
# - Does the title contain a plausible episode token?
# - Does the title contain the episode title?
# - Does the duration look like an episode instead of a short/clip?
# - Does it contain bad words like trailer/reaction/compilation?
#
# The output includes a normalized_title field that is much closer
# to what Sonarr would want to see.
jq -c \
  --arg series "$SERIES" \
  --arg ep_title "$PRIMARY_TITLE" \
  --argjson ep_titles "$EP_TITLES_JSON" \
  --arg season "$SEASON" \
  --arg episode "$EPISODE" \
  --argjson min_duration "$MIN_DURATION" \
  --argjson expected_duration "${EXPECTED_DURATION:-0}" \
  --argjson ep_tokens "$EP_TOKENS_JSON" '
  def norm:
    tostring
    | ascii_downcase
    | gsub("[^a-z0-9]+"; " ")
    | gsub("^ +| +$"; "");


  def pad2:
    tostring | if length < 2 then "0" + . else . end;


  ((.title // "") | norm) as $t_norm |
  ((.title // "") | ascii_downcase) as $t_lower |


  ($series | norm) as $s_norm |


  ($season | tostring | pad2) as $sp |
  ($episode | tostring | pad2) as $ep |


  # Series match score.
  # If the series name has multiple words, reward partial matches,
  # but cap the score so it cannot dominate everything.
  ($s_norm | split(" ") | map(select(length > 0))) as $s_words |
  (
    if ($s_words | length) == 0 then
      0
    else
      ([ $s_words[] | select(. as $w | $t_norm | contains($w)) ] | length) as $matched |
      ((100 * $matched / ($s_words | length)) | floor | if . > 45 then 45 else . end)
    end
  ) as $series_score |


  # Episode token score.
  ([ $ep_tokens[] | select(. as $tok | $t_lower | contains($tok)) ] | length) as $ep_tok_count |
  (
    if $ep_tok_count > 0 then
      35
    else
      0
    end
  ) as $episode_score |


  # Episode title match score: the best score across all provided titles
  # (primary + localized alternatives).  A full-title hit earns 20; otherwise
  # partial word matches scale up to 20.
  (
    if ($ep_titles | length) == 0 then
      0
    else
      ([ $ep_titles[] |
        ((. | norm) as $et |
         if $et == "" then
           0
         elif ($t_norm | contains($et)) then
           20
         else
           ($et | split(" ") | map(select(length >= 3))) as $et_words |
           if ($et_words | length) == 0 then
             0
           else
             ([ $et_words[] | select(. as $w | $t_norm | contains($w)) ] | length) as $matched |
             ((20 * $matched / ($et_words | length)) | floor)
           end
         end)
      ] | max)
    end
  ) as $title_score |


  # Duration sanity score.
  # When an expected runtime is known the window filter already excluded
  # anything outside it, so every survivor is in range and duration is not
  # ranked (constant 5).  In the fallback (no expected duration) the classic
  # range score applies: MIN_DURATION (default 5 min) up to 2h, with
  # penalties scaling with extremity for clips/shorts and compilations.
  ((.duration // 0) | if type == "number" then . else (try tonumber catch 0) end) as $dur |
  (
    if $expected_duration > 0 then
      5
    elif $dur >= $min_duration and $dur <= 7200 then
      5
    elif $dur > 0 and $dur < $min_duration then
      ((-25 * (($min_duration - $dur) / $min_duration)) | floor)
    elif $dur > 7200 then
      ((-25 * (($dur - 7200) / 7200)) | floor | if . < -40 then -40 else . end)
    else
      0
    end
  ) as $duration_score |


  # Penalize obvious non-episode content.
  [
    "trailer",
    "teaser",
    "reaction",
    "review",
    "commentary",
    "compilation",
    "moments",
    "behind the scenes",
    "shorts",
    "short",
    "clip",
    "highlights",
    "full movie",
    "full episodes",
    "all episodes",
    "collection",
    "recap",
    "ending",
    "scene",
    "moment",
    "song",
    "audio"
  ] as $bad_words |


  ([ $bad_words[] | select(. as $w | $t_lower | contains($w)) ] | length) as $bad_count |
  (
    if $bad_count > 0 then
      -35
    else
      0
    end
  ) as $negative_score |


  # Small bonus if the channel name contains the series name.
  ((.channel // "") | norm) as $c_norm |
  (
    if $s_norm != "" and ($c_norm | contains($s_norm)) then
      5
    else
      0
    end
  ) as $channel_score |


  ($series_score + $episode_score + $title_score + $duration_score + $negative_score + $channel_score) as $score |


  # A result is marked probable only if it has a decent score and
  # some episode/title evidence.
  (
    if $score >= 70 and ($episode_score > 0 or $title_score >= 15) then
      true
    else
      false
    end
  ) as $probable |


  # A normalized Sonarr-friendly title.
  # The TVDB episode title is matched against but intentionally NOT shown in
  # the release title — the indexer puts the YouTube channel there instead.
  (
    (
      [
        $series,
        ("S" + $sp + "E" + $ep),
        "WEB"
      ]
      | map(select(. != null and . != ""))
      | join(" ")
    )
    | gsub("\\s+"; " ")
  ) as $normalized_title |


  {
    score: $score,
    probable: $probable,
    has_episode: ($episode_score > 0),
    normalized_title: $normalized_title,
    id: .id,
    title: .title,
    url: .url,
    duration: $dur,
    timestamp: (.timestamp // 0),
    channel: .channel,
    views: .views,
    series_score: $series_score,
    episode_score: $episode_score,
    title_score: $title_score,
    duration_score: $duration_score,
    negative_score: $negative_score,
    channel_score: $channel_score,
    queries: .queries
  }
' "$DEDUP" > "$SCORED"


# Sort and limit final results.  Number-matched episodes (has_episode) always
# sort above title-only candidates; within each tier, score decides.
jq -c -s --argjson max "$MAX_RESULTS" '
  sort_by([if .has_episode then 0 else 1 end, -.score])
  | .[0:$max]
  | .[]
' "$SCORED" > "$SORTED"


if [[ ! -s "$SORTED" ]]; then
  echo "No results found." >&2
  if [[ -s "$ERRLOG" ]]; then
    echo "yt-dlp errors:" >&2
    tail -n 20 "$ERRLOG" >&2
  fi
  exit 1
fi


# Optional quality pass: re-probe the top RESOLVE_TOP candidates directly to
# recover resolution and refresh the publish timestamp.  Flat playlist search
# results never carry resolution; a full --dump-json adds it (~1-2s/video).
# Set RESOLVE_TOP=0 to skip.
if [[ "$RESOLVE_TOP" -gt 0 ]] && command -v xargs >/dev/null 2>&1; then
    head -n "$RESOLVE_TOP" "$SORTED" | jq -r '.url // empty' |
      xargs -P 4 -I{} sh -c \
        'yt-dlp --ignore-config --dump-json --no-warnings --skip-download --retries 3 --extractor-args "youtube:player_client=$PLAYER_CLIENT" $(if [ -n "$POT_PROVIDER" ]; then printf -- "--extractor-args youtubepot-bgutilhttp:base_url=$POT_PROVIDER"; fi) "$1" 2>/dev/null | jq -c --arg url "$1" '"'"'{url: $url, height: (.height // 0), timestamp: (.timestamp // 0), language: (.language // "")}'"'"' ' _ {} \
        > "$RESOLVED" 2>/dev/null || true
  jq -c -s --slurpfile meta "$RESOLVED" '
    def rfc2822($ts):
      if ($ts // 0) > 0 then (($ts | strftime("%a, %d %b %Y %H:%M:%S")) + " +0000") else "" end;
    def qlabel($h):
      if $h >= 2160 then "2160p"
      elif $h >= 1440 then "1440p"
      elif $h >= 1080 then "1080p"
      elif $h >= 720 then "720p"
      elif $h >= 480 then "480p"
      else "360p" end;
    ($meta | map({key: .url, value: .}) | from_entries) as $m |
    .[] |
    . as $item |
    ($m[.url] // {} | {height: (.height // 0), timestamp: (.timestamp // 0), language: (.language // "")}) as $r |
    ((if $r.height > 0 then qlabel($r.height) else null end)) as $res |
    (((if $r.timestamp > 0 then $r.timestamp else $item.timestamp end))) as $ts |
    $item +
    {
      resolution: $res,
      language: ($r.language // ""),
      pub_date: rfc2822($ts),
      normalized_title: (
        if $res then (.normalized_title + " " + $res) else .normalized_title end
      )
    }
  ' "$SORTED" > "$FINAL"
else
  # No resolution pass: still emit pub_date from the flat search timestamp.
  jq -c -s '
    def rfc2822($ts):
      if ($ts // 0) > 0 then (($ts | strftime("%a, %d %b %Y %H:%M:%S")) + " +0000") else "" end;
    .[] |
    . + { pub_date: rfc2822(.timestamp) }
  ' "$SORTED" > "$FINAL"
fi


BEST_JSON="$(
  jq -s '
    (map(select(.probable)) | .[0]) // .[0]
  ' "$FINAL"
)"


if [[ -z "$BEST_JSON" || "$BEST_JSON" == "null" ]]; then
  echo "No usable candidate." >&2
  exit 1
fi


if $BEST_ONLY; then
  jq -r '.url // empty' <<<"$BEST_JSON"
  exit 0
fi


if $DOWNLOAD; then
  url="$(jq -r '.url // empty' <<<"$BEST_JSON")"
  if [[ -z "$url" ]]; then
    echo "No candidate URL to download." >&2
    exit 1
  fi


  mkdir -p "$DOWNLOAD_DIR"


  echo "Downloading best candidate:" >&2
  echo "URL: $url" >&2


  # You may want to adjust format, cookies, sponsor-block, etc.
  yt-dlp \
    --ignore-config \
    --restrict-filenames \
    --format "bv*+ba/b" \
    --extractor-args "youtube:player_client=$PLAYER_CLIENT" \
    --output "$DOWNLOAD_DIR/%(title)s [%(id)s].%(ext)s" \
    "$url"


  exit 0
fi


if $JSON_OUT; then
  cat "$FINAL"
else
  jq -r '
    [
      .score,
      .probable,
      .duration,
      (.channel // ""),
      .title,
      .url
    ]
    | @tsv
  ' "$FINAL" |
    if command -v column >/dev/null 2>&1; then
      column -t -s $'\t'
    else
      cat
    fi
fi
