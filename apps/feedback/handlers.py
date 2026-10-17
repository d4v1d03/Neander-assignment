from neai import AppError


def create_post(ctx, args):
    text = args["text"]
    if not text.strip():
        raise AppError("INVALID_ARGS", "text must not be blank")
    post = ctx.db.insert("posts", {"author": ctx.caller, "text": text})
    return _with_votes(ctx, post)


def list_posts(ctx, args):
    return [_with_votes(ctx, post) for post in ctx.db.find("posts")]


def vote(ctx, args):
    post_id = args["post_id"]
    if ctx.db.get("posts", post_id) is None:
        raise AppError("POST_NOT_FOUND", f"no post with id {post_id!r}")
    already = ctx.db.count("votes", where={"post_id": post_id, "voter": ctx.caller}) > 0
    if not already:
        ctx.db.insert("votes", {"post_id": post_id, "voter": ctx.caller})
    return {
        "post_id": post_id,
        "counted": not already,
        "votes": ctx.db.count("votes", where={"post_id": post_id}),
    }


def delete_post(ctx, args):
    post_id = args["post_id"]
    if ctx.db.get("posts", post_id) is None:
        raise AppError("POST_NOT_FOUND", f"no post with id {post_id!r}")
    for v in ctx.db.find("votes", where={"post_id": post_id}):
        ctx.db.delete("votes", v["id"])
    ctx.db.delete("posts", post_id)
    ctx.log("post deleted", id=post_id, by=ctx.caller)
    return {"deleted": post_id}


def _with_votes(ctx, post):
    # list_posts is public: an anonymous reader (ctx.caller is None) has voted on nothing.
    mine = ctx.caller is not None and ctx.db.count("votes", where={"post_id": post["id"], "voter": ctx.caller}) > 0
    return {**post, "votes": ctx.db.count("votes", where={"post_id": post["id"]}), "voted_by_me": mine}
