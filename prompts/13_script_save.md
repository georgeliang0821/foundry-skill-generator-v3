## Saving a Script Skill

Saving a script skill re-reads the EAA flags `DYNAMIC_SKILLS_ENABLED` and
`SKILL_SCRIPTS_ENABLED` first. When either is off, the save is refused with
`script_flags_off` ("... is not true on the EAA deployment ... Save again once
the flags are on.") and nothing is written. When the user reports that:

- Tell them which flag is off and that an EAA administrator has to turn it on;
  once it is on, they save again. The draft and the script stay in the session
  as they are, so nothing else needs to change.
- Do not suggest converting the skill to inline sample code or rebuilding it in
  a new session. The flag is a deployment setting, not a defect in the skill.
