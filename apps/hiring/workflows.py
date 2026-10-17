# Python that workflows.json names. Hooks run inside the same transaction as the move that
# triggered them: if one fails, the move is undone too.

SALARY_BANDS = {"SDE1": 1_200_000, "SDE2": 2_000_000, "SDE3": 3_200_000}


def prepare_offer(ctx, application):
    """on_enter for "hr": draft the offer the candidate will see once they reach HR."""
    salary = SALARY_BANDS[application["level"]]
    if application["experience_years"] >= 5:
        salary = salary * 11 // 10
    ctx.db.insert("offers", {"application_id": application["id"], "candidate": application["candidate"],
                             "level": application["level"], "salary": salary})
    ctx.log("offer prepared", application=application["id"], salary=salary)
