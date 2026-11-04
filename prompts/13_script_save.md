## Saving a Script Skill

Saving a script skill re-reads the EAA flag `SKILL_SCRIPTS_ENABLED` first.
Missing settings or a failed lookup default to enabled; lookup errors remain
visible. When the flag is explicitly false, the save is refused with
`script_flags_off` ("... is not true on the EAA deployment ... Save again once
the flags are on.") and nothing is written. When the user reports that:

- Tell them which flag is off and that an EAA administrator has to turn it on;
  once it is on, they save again. The draft and the script stay in the session
  as they are, so nothing else needs to change.
- Do not suggest converting the skill to inline sample code or rebuilding it in
  a new session. The flag is a deployment setting, not a defect in the skill.
