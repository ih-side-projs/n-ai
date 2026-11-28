-- Language selection for the pipeline.
--
-- ``project.settings.language`` selects the LanguageAdapter.  Every existing
-- project keeps the Python behaviour it was built with, and new projects get
-- the same default when the key is absent (see ``nai.langs.registry``).
UPDATE projects
   SET settings_json = json_set(settings_json, '$.language', 'python')
 WHERE json_valid(settings_json)
   AND json_extract(settings_json, '$.language') IS NULL;
