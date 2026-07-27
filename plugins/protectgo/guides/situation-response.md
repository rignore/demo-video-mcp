# Protect GO situation response recording

This guide belongs to the optional `protectgo` plugin. It is not part of the
generic MCP core.

## Inputs

- `origin`
- `project_name`
- `target_title`
- `target_situation_id`
- `summary`
- `description`
- Login profile captured against the same origin

## Preconditions

- The selected project matches `project_name`.
- The target situation and notification still exist.
- The target is in a disposable test project when recording mutation steps.

## External effects

Opening the action-history route can assign the current user. Saving the
summary and completing the final form update remote workflow data. These
steps must never run without an exact plan-hash approval and must never be
automatically retried.
