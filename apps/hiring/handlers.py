from neai import AppError

LEVELS = ("SDE1", "SDE2", "SDE3")


def list_jobs(ctx, args):
    return ctx.db.find("jobs", where={"open": True})


def post_job(ctx, args):
    if not args["title"].strip():
        raise AppError("INVALID_ARGS", "title must not be blank")
    if args["level"] not in LEVELS:
        raise AppError("INVALID_ARGS", f"level must be one of {', '.join(LEVELS)}")
    return ctx.db.insert("jobs", {"title": args["title"], "level": args["level"]})


def close_job(ctx, args):
    _job(ctx, args["job_id"])
    return ctx.db.update("jobs", args["job_id"], {"open": False})


def apply(ctx, args):
    job = _job(ctx, args["job_id"])
    if not job["open"]:
        raise AppError("JOB_CLOSED", f"'{job['title']}' is no longer taking applications")
    if not 0 <= args["experience_years"] <= 60:
        raise AppError("INVALID_ARGS", "experience_years must be between 0 and 60")
    if ctx.db.count("applications", where={"candidate": ctx.caller, "job_id": job["id"]}):
        raise AppError("ALREADY_APPLIED", f"you already applied to '{job['title']}'")
    # No "stage" here: the workflow puts every new application in its first round.
    return ctx.db.insert("applications", {"candidate": ctx.caller, "job_id": job["id"], "level": job["level"],
                                          "experience_years": args["experience_years"]})


def my_applications(ctx, args):
    out = []
    for app in ctx.db.find("applications", where={"candidate": ctx.caller}):
        job = ctx.db.get("jobs", app["job_id"])
        offer = ctx.db.find("offers", where={"application_id": app["id"]}, limit=1)
        out.append({**app, "job_title": job["title"] if job else None, "offer": offer[0] if offer else None})
    return out


def withdraw(ctx, args):
    app = _application(ctx, args["id"])
    if app["candidate"] != ctx.caller:
        raise AppError("APPLICATION_NOT_FOUND", f"no application with id {args['id']!r}")
    return ctx.workflow.exit("hiring", app["id"], "withdrawn")


def list_applications(ctx, args):
    where = {"stage": args["stage"]} if "stage" in args else None
    return ctx.db.find("applications", where=where)


def pass_round(ctx, args):
    app = _application(ctx, args["id"])
    # The workflow checks who may pass this round, skips rounds whose rule matches, and runs hooks.
    return ctx.workflow.advance("hiring", app["id"], note=args.get("note"))


def reject(ctx, args):
    app = _application(ctx, args["id"])
    return ctx.workflow.exit("hiring", app["id"], "rejected", note=args.get("note"))


def history(ctx, args):
    app = _application(ctx, args["id"])
    if app["candidate"] != ctx.caller and not (ctx.has_role("recruiter") or ctx.has_role("interviewer")):
        raise AppError("APPLICATION_NOT_FOUND", f"no application with id {args['id']!r}")
    return {"application": app, "moves": ctx.workflow.history("hiring", app["id"])}


def _job(ctx, job_id):
    job = ctx.db.get("jobs", job_id)
    if job is None:
        raise AppError("JOB_NOT_FOUND", f"no job with id {job_id!r}")
    return job


def _application(ctx, app_id):
    app = ctx.db.get("applications", app_id)
    if app is None:
        raise AppError("APPLICATION_NOT_FOUND", f"no application with id {app_id!r}")
    return app
