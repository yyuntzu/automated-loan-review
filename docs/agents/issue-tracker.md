# Issue tracker: GitHub

Issues/specs: https://github.com/yyuntzu/automated-loan-review/issues

Use `gh` with `--repo yyuntzu/automated-loan-review`.

- Read: `gh issue view <number> --json number,title,body,labels,comments`
- Create: `gh issue create --title "..." --body-file <path>`
- Edit labels: `gh issue edit <number> --add-label <label>`

PRs as a request surface: off.

Create or update issues only when authorized by the user's task or invoked skill.
