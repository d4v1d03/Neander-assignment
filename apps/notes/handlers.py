from neai import AppError


def create_note(ctx, args):
    record = {"owner": ctx.caller, "title": _title(args)}
    if "body" in args:
        record["body"] = args["body"]
    note = ctx.db.insert("notes", record)
    ctx.log("note created", id=note["id"])
    return note


def list_notes(ctx, args):
    return ctx.db.find("notes", where={"owner": ctx.caller})


def get_note(ctx, args):
    return _own_note(ctx, args["id"])


def update_note(ctx, args):
    note = _own_note(ctx, args["id"])
    changes = {}
    if "title" in args:
        changes["title"] = _title(args)
    if "body" in args:
        changes["body"] = args["body"]
    if not changes:
        raise AppError("INVALID_ARGS", "nothing to update: pass title and/or body")
    return ctx.db.update("notes", note["id"], changes)


def _own_note(ctx, note_id):
    note = ctx.db.get("notes", note_id)
    if note is None or note["owner"] != ctx.caller:
        raise AppError("NOTE_NOT_FOUND", f"no note with id {note_id!r}")
    return note


def _title(args):
    title = args["title"]
    if not title.strip():
        raise AppError("INVALID_ARGS", "title must not be blank")
    return title
