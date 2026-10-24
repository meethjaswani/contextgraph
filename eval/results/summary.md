| Repo | Mode | Fully correct | Answer recall | Avg tokens | Avg files read | Avg tool calls | Avg time (s) |
|---|---|---|---|---|---|---|---|
| click | baseline | 2/5 (40%) | 66% | 10,840 | 1.2 | 5.4 | 60.8 |
| click | contextgraph | 2/5 (40%) | 56% | 11,478 | 0.8 | 5.6 | 68.1 |
| requests | baseline | 1/5 (20%) | 30% | 11,596 | 1.6 | 7.0 | 70.1 |
| requests | contextgraph | 4/5 (80%) | 80% | 9,179 | 0.8 | 4.6 | 55.9 |
| rich | baseline | 5/5 (100%) | 100% | 3,694 | 0.8 | 2.4 | 23.0 |
| rich | contextgraph | 5/5 (100%) | 100% | 4,357 | 0.2 | 1.8 | 70.0 |
| **all** | baseline | 8/15 (53%) | 65% | 8,710 | 1.2 | 4.9 | 51.3 |
| **all** | contextgraph | 11/15 (73%) | 79% | 8,338 | 0.6 | 4.0 | 64.6 |

| Question | Model | Baseline | ContextGraph | Baseline tokens | ContextGraph tokens |
|---|---|---|---|---|---|
| req-01 | openai/gpt-oss-20b | missed merge_environment_settings | missed merge_environment_settings, resolve_proxies | 3,607 | 27,204 |
| req-02 | openai/gpt-oss-20b | correct | correct | 11,600 | 12,064 |
| req-04 | openai/gpt-oss-20b | missed resolve_redirects | correct | 19,408 | 2,013 |
| req-05 | openai/gpt-oss-20b | missed Session.send | correct | 4,556 | 2,167 |
| req-07 | openai/gpt-oss-20b | missed prepare_body, prepare_content_length | correct | 18,807 | 2,449 |
| click-01 | openai/gpt-oss-20b | correct | correct | 1,814 | 11,030 |
| click-02 | openai/gpt-oss-20b | missed resolve_command, _parse_decls, get_help_record, join_options, normalize_opt, __init__ | missed resolve_command, _parse_decls, get_help_record, join_options, normalize_opt, __init__ | 27,507 | 24,126 |
| click-05 | openai/gpt-oss-20b | missed Context | missed auto_envvar_prefix, Context | 4,828 | 16,931 |
| click-07 | qwen/qwen3.8-27b | correct | correct | 2,423 | 2,456 |
| click-08 | qwen/qwen3.8-27b | missed _process_result | missed forward | 17,628 | 2,846 |
| rich-01 | qwen/qwen3.8-27b | correct | correct | 2,280 | 3,262 |
| rich-03 | qwen/qwen3.8-27b | correct | correct | 3,007 | 2,548 |
| rich-04 | qwen/qwen3.8-27b | correct | correct | 3,451 | 9,839 |
| rich-06 | qwen/qwen3.8-27b | correct | correct | 8,041 | 3,517 |
| rich-10 | qwen/qwen3.8-27b | correct | correct | 1,690 | 2,617 |
