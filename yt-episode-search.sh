#!/usr/bin/env bash
set -euo pipefail


usage() {
  cat <<'EOF'
Usage:
  ./yt-episode-search.sh -s "Series Name" -S 1 -E 2 -t "Episode Title" [options]


Options:
  -s SERIES      Series name (required)
  -S SEASON      Season number (required)
  -E EPISODE     Episode number (required)
  -t TITLE       Episode title (optional but strongly recommended)
  -n MAX         Max final results to output (default: 20)
  -p PER_QUERY   Number of results per YouTube query (default: 5)
  -d DELAY       Delay between queries in seconds (default: 1)
  -j             Output JSON lines instead of a table
  -b             Print best candidate URL only
  -D             Download best candidate with yt-dlp
  -o DIR         Download directory, used with -D (default: downloads)
  -h             Show help


Environment:
  MIN_DURATION   Minimum video length in seconds (default: 300)
  RESOLVE_TOP    Number of top candidates to re-probe for resolution
                 metadata (default: 5, 0 disables the pass)


Examples:
  ./yt-episode-search.sh -s "My Show" -S 1 -E 2 -t "The Cave"
  ./yt-episode-search.sh -s "My Show" -S 1 -E 2 -t "The Cave" -j
  ./yt-episode-search.sh -s "My Show" -S 1 -E 2 -t "The Cave" -b
  ./yt-episode-search.sh -s "My Show" -S 1 -E 2 -t "The Cave" -D
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
EP_TITLE=""
MAX_RESULTS=20
PER_QUERY=5
DELAY=1
MIN_DURATION=${MIN_DURATION:-300}
RESOLVE_TOP=${RESOLVE_TOP:-5}
JSON_OUT=false
BEST_ONLY=false
DOWNLOAD=false
DOWNLOAD_DIR="downloads"


while getopts ":s:S:E:t:n:p:d:o:Dbjh" opt; do
  case "$opt" in
    s) SERIES="$OPTARG" ;;
    S) SEASON="$OPTARG" ;;
    E) EPISODE="$OPTARG" ;;
    t) EP_TITLE="$OPTARG" ;;
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


if ! [[ "$EPISODE" =~ ^[0-9]+$ ]]; then
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


# Normalize leading zeros: 01 -> 1
SEASON=$((10#$SEASON))
EPISODE=$((10#$EPISODE))


require_cmd yt-dlp
require_cmd jq


SE_TAG=$(printf "S%02dE%02d" "$SEASON" "$EPISODE")


# Build multiple search variants.
# YouTube search is noisy, so more query shapes usually help.
queries=(
  "$SERIES $SE_TAG"
  "$SERIES season $SEASON episode $EPISODE"
  "$SERIES episode $EPISODE"
)


if [[ -n "$EP_TITLE" ]]; then
  queries+=(
    "$SERIES $SE_TAG $EP_TITLE"
    "$SERIES $EP_TITLE"
    "$SERIES episode $EPISODE $EP_TITLE"
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


: > "$RAW"
: > "$ERRLOG"


echo "Searching YouTube..." >&2


for q in "${queries[@]}"; do
  echo "Query: $q" >&2


  # --flat-playlist makes search much faster because it does not resolve every video fully.
  # --dump-json prints one JSON object per result.
  yt-dlp \
    --ignore-config \
    --flat-playlist \
    --dump-json \
    --no-warnings \
    --retries 3 \
    --match-filter "duration >= ${MIN_DURATION}" \
    "ytsearch${PER_QUERY}:${q}" 2>>"$ERRLOG" |
    jq -c --arg query "$q" --argjson min_duration "$MIN_DURATION" '
      select(.id != null) |
      select((.duration // 0) >= $min_duration) |
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
    ' >> "$RAW" || true


  sleep "$DELAY"
done


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
  --arg ep_title "$EP_TITLE" \
  --arg season "$SEASON" \
  --arg episode "$EPISODE" \
  --argjson min_duration "$MIN_DURATION" \
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
  ($ep_title | norm) as $et_norm |


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


  # Episode title match score.
  (
    if $et_norm == "" then
      0
    elif ($t_norm | contains($et_norm)) then
      20
    else
      ($et_norm | split(" ") | map(select(length >= 3))) as $et_words |
      if ($et_words | length) == 0 then
        0
      else
        ([ $et_words[] | select(. as $w | $t_norm | contains($w)) ] | length) as $matched |
        ((20 * $matched / ($et_words | length)) | floor)
      end
    end
  ) as $title_score |


  # Duration sanity score.
  # Good range: MIN_DURATION (default 5 min) up to 2h.
  # Penalties scale with extremity: well under the 5-minute floor is almost
  # certainly a clip/short; well over 2h is likely a compilation/live stream.
  ((.duration // 0) | if type == "number" then . else (try tonumber catch 0) end) as $dur |
  (
    if $dur >= $min_duration and $dur <= 7200 then
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
  # This is not guaranteed to be correct; it is a candidate title.
  (
    (
      [
        $series,
        ("S" + $sp + "E" + $ep),
        (if $ep_title != "" then $ep_title else empty end),
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


# Sort and limit final results.
jq -c -s --argjson max "$MAX_RESULTS" '
  sort_by(-.score)
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
      'yt-dlp --ignore-config --dump-json --no-warnings --skip-download --retries 3 "$1" 2>/dev/null | jq -c --arg url "$1" '"'"'{url: $url, height: (.height // 0), timestamp: (.timestamp // 0)}'"'"' ' _ {} \
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
    ($m[.url] // {} | {height: (.height // 0), timestamp: (.timestamp // 0)}) as $r |
    $item +
    {
      resolution: (if $r.height > 0 then qlabel($r.height) else null end),
      pub_date: rfc2822((if $r.timestamp > 0 then $r.timestamp else $item.timestamp end))
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
