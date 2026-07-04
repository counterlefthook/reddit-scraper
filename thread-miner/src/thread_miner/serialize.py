"""Comment tree -> chunk text for LLM extraction (spec section 13.1)."""

from __future__ import annotations


def _line(c, trunc: int) -> str:
    body = " ".join((c["body"] or "").split())[:trunc]
    author = c["author"] or "[deleted]"
    return f'{"  " * c["depth"]}[{c["comment_fullname"]}|s={c["score"]}|{author}] {body}'


def _ctx_line(c, trunc: int = 300) -> str:
    body = " ".join((c["body"] or "").split())[:trunc]
    return f'{"  " * c["depth"]}[ctx {c["comment_fullname"]}] {body}'


def serialize_thread(post_row, comment_rows, cfg: dict) -> list[str]:
    """Serialize one thread into extraction chunks.

    Takes the top MAX_COMMENTS_ANALYZED comments by score, restores tree
    order, groups whole top-level subtrees into chunks of at most
    CHUNK_CHAR_LIMIT chars, and splits oversized subtrees at depth boundaries
    with parent context (ctx) lines carried into the continuation chunk.
    """
    t = cfg["thresholds"]
    max_comments = t["MAX_COMMENTS_ANALYZED"]
    trunc = t["COMMENT_BODY_TRUNC"]
    limit = t["CHUNK_CHAR_LIMIT"]

    selected = {
        c["comment_fullname"]
        for c in sorted(comment_rows, key=lambda c: c["score"] or 0, reverse=True)[:max_comments]
    }
    ordered = [c for c in comment_rows if c["comment_fullname"] in selected]

    header = (
        f"POST TITLE: {post_row['title']}\n"
        f"POST BODY: {(post_row['selftext'] or '')[:1500]}\n---\n"
    )

    # Split into top-level subtrees (a new subtree starts at depth 0).
    subtrees: list[list] = []
    for c in ordered:
        if c["depth"] == 0 or not subtrees:
            subtrees.append([])
        subtrees[-1].append(c)

    chunks: list[str] = []
    current: list[str] = []
    current_len = len(header)

    def flush():
        nonlocal current, current_len
        if current:
            chunks.append(header + "\n".join(current))
        current = []
        current_len = len(header)

    for subtree in subtrees:
        lines = [_line(c, trunc) for c in subtree]
        subtree_len = sum(len(l) + 1 for l in lines)

        if subtree_len > limit - len(header):
            # Oversized subtree: split at depth boundaries with parent context.
            flush()
            ancestors: dict[int, object] = {}
            part: list[str] = []
            part_len = len(header)
            for c, line in zip(subtree, lines):
                ancestors[c["depth"]] = c
                if part_len + len(line) + 1 > limit and part:
                    chunks.append(header + "\n".join(part))
                    ctx = [
                        _ctx_line(ancestors[d]) for d in sorted(ancestors)
                        if d < c["depth"]
                    ]
                    part = ctx[:]
                    part_len = len(header) + sum(len(l) + 1 for l in part)
                part.append(line)
                part_len += len(line) + 1
            if part:
                chunks.append(header + "\n".join(part))
            continue

        if current_len + subtree_len > limit:
            flush()
        current.extend(lines)
        current_len += subtree_len
    flush()
    return chunks
