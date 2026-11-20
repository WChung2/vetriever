# Codebook (schema v2)

Generated from `codebook.csv` and the `[[fields]]` of each `topics/<topic>.toml` by
`vet templates`. Edit those, not this file.

## Field kinds
- **K key**: identifiers and links.
- **T transcribed**: copied from the document as stated; empty if the document does not state it.
- **C coded**: one of the listed values, assigned by the written rule. A filled C field always
  has an evidence row with its supporting quote.
- **N note**: the extractor's own words (`studies.notes`, `conflicts.note`); never checked
  against the text.
- **D derived**: computed downstream by our code. Never extracted.

## Topics
A topic is one line of interest, such as post-surgical pain in dogs, described in
`topics/<topic>.toml`. A study can belong to several topics (`study_topics`). Fields that
matter for one topic only are defined in its file (listed at the end of this codebook) and
stored one value per row in `topic_fields`; every other field applies to every study.

## Missing values
An empty cell always means "not stated". "Not applicable" is recorded only by the
`applies_to` column below (which fields apply to which study kind; when that column is
empty the field still applies, since an unstated design can't rule it out) and by
`studies.not_applicable` for one-off exceptions. Never write NA, N/A, NULL, none, -,
"not reported" or "not stated" in a cell; only a listed value may say `unknown` or `none`.

## Keys (letters A-Z, digits and hyphens, uppercase)
A document's `source_id` is set once, when the document first enters the database, by the
first rule that applies. It never changes: the database finds the document again by any of
its DOI, PMID or PMCID, so the same paper is never stored twice.

| Document | source_id |
|---|---|
| has a DOI (not a conference abstract) | `DOI-10-1111-VAA-12345` (each run of other characters becomes one `-`) |
| has a PMID but no DOI | `PMID31234567` |
| FDA CVM FOI summary | `FDA-NADA141455` / `FDA-ANADA200123` |
| EMA EPAR | `EMA-EMEAVC000123` |
| registry entry | `REG-AVMA-1234` |
| conference abstract (abstracts in one supplement can share a DOI) | `ABS-ACVIM2023-GI12` |
| agency or web page | `WEB-WSAVA-20240115-1` |

Add `-2`, `-3`… if two documents would collide.
Study `ID` = `VET-<n>`: numbered in the order studies enter the database, with no fixed
width, so it never runs out. It is not built from any document, so it stays the same when more
documents about the study arrive (each one is a row in `study_sources`). `arm_id` =
`<ID>-A<n>`; `dose_id` = `<arm_id>-D<n>`; `result_id` = `<ID>-R<n>`; `site_no` and
`person_no` are integers unique within a study.

## Formats
- CSV, UTF-8, standard quoting, header row in template order, no index column.
- Lists: pipe-separated, no spaces around the pipe. Country lists sorted alphabetically,
  names from `vocab/countries.txt` (the ClinicalTrials.gov names the app already uses).
- Numbers: digits and a dot only; no units, thousands separators, `~` or `≥`. If only an
  approximate value or a range is given where one number is expected, leave it empty and
  explain in notes.
- Dates: ISO at the precision stated (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`).
- Verbatim fields: original language and spelling, symbols kept (≥, µg, °C).
- Eligibility Criteria: an `Inclusion Criteria:` header, a blank line, one `* ` line per
  criterion, then the same for `Exclusion Criteria:`.

## Evidence
`evidence = quote`: needs an evidence row with a verbatim quote (≤500 characters, or
`quote_truncated = yes`). `evidence = location`: needs an evidence row with its location
only. `results` and `definitions` rows carry their own text and location. For a topic field
the evidence row has `table = topic_fields`, `row_key = <topic>` and `field = <field name>`.
A value may have several evidence rows (one per document reporting it, or one per part of a
list, such as a quote for each country); two identical evidence rows are a duplicate.

## Conflicts
When two documents about one study, or two places in one document, give different values for
the same cell, the table keeps the primary document's value and `conflicts` records both.

## One experiment in several papers
Each paper extracted on its own gives its own study rows, with its own outcomes and results.
When two of them report one experiment (the same animals, groups and treatments: a paper and
a sub-study of its samples, or a later report with longer follow-up), both rows stay and
`study_links` joins them with `relation = same_experiment`: count them once. Animals that
take part in two different experiments (a research colony used again, overlapping
enrolment) are `shared_animals`: not independent. Both need a document's own words, checked
against its text. `distinct` records that two look-alike studies were checked and are
separate, so they are not flagged again.

## Doses
`dose_value` is the dose as prescribed or planned, or, when the document reports the doses
the animals received as a summary (2.0 ± 0.1 mg/kg, a median and its range), that mean or
median; `dose_statistic` says which. The spread, a taper or a weight band stays in
`dose_as_written`. Any unit, as written: mg/kg, mg/m2, µg/kg/min, mg per dog, IU/kg.

## Working rules
Each document is extracted alone, from its saved text `docs/<source_id>.txt`, into
`out/<source_id>/`. Copy, don't paraphrase. Never fill a gap from another source or from
memory, never read values off figures, never compute, convert, round, pool or total.
When unsure, leave the field empty and say why in notes.


## sources

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| source_id | K | text |  | Document key (see Keys in codebook.md). | none | all |
| doc_type | C | enum | journal_article, conference_abstract, fda_foi_summary, ema_epar, consensus_statement, registry_entry, agency_page, thesis, other | Kind of document. As shown on the document or its index record. | none | all |
| title | T | text |  | Title as written. | none | all |
| first_author | T | text |  | First author as written. | none | all |
| last_author | T | text |  | Last author as written. | none | all |
| year | T | year |  | Publication year. | none | all |
| journal_or_agency | T | text |  | Journal or issuing agency as written. | none | all |
| doi | T | text |  | DOI copied from the document or its index record, never typed from memory. | none | all |
| pmid | T | integer |  | PMID copied from the index record, never typed from memory. | none | all |
| pmcid | T | text |  | PubMed Central ID (PMC1234567). | none | all |
| url | T | text |  | Where the document was retrieved. | none | all |
| language | C | enum | en, zh, ja, ko, de, fr, es, it, pt, nl, pl, ru, sv, da, no, fi, cs, sk, hu, ro, tr, el, he, ar, fa, th, vi, id, ms, hr, sl, sr, bg, uk, lt, lv, et, ca | Language of the main text. ISO 639-1 code. | none | all |
| access | C | enum | open_access, paywalled, abstract_only | How the text was obtained. open_access = full text freely and legally available; paywalled = full text under a subscription; abstract_only = only the abstract was available. | none | all |
| file_name | T | text |  | docs/&lt;source_id&gt;.&lt;pdf\|xml\|html&gt; | none | all |
| text_file | T | text |  | docs/&lt;source_id&gt;.txt | none | all |
| retrieved_date | T | date |  | Date retrieved, YYYY-MM-DD. | none | all |

## study_sources

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key VET-&lt;n&gt;. | none | all |
| source_id | K | text |  | A document reporting this study. | none | all |
| role | C | enum | primary, secondary, regulatory | The document's role for this study. primary = the document that reports the study most fully; regulatory = an FDA or EMA summary of the study; secondary = any other document reporting it. Exactly one primary per study. | none | all |

## studies

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key VET-&lt;n&gt;. | none | all |
| study_kind | C | enum | prospective_trial, prospective_observational, retrospective_cohort, case_control, cross_sectional, case_series, case_report, other | Kind of study. By ClinicalTrials.gov's Time Perspective and Observational Study Model; prospective_trial = an interventional study. | quote | all |
| study_stage | C | enum | pharmacokinetic, safety, dose_determination, exploratory, confirmatory, other | Development stage. As ICH E8 classifies studies by objective; dose_determination as VICH defines it. | quote | Study Type = INTERVENTIONAL |
| Study Type | C | enum | INTERVENTIONAL, OBSERVATIONAL | App study type. As ClinicalTrials.gov defines Study Type. | quote | all |
| Overall Status | C | enum | COMPLETED, TERMINATED, RECRUITING, ACTIVE_NOT_RECRUITING, UNKNOWN | Study status. As ClinicalTrials.gov defines Overall Recruitment Status. | quote | all |
| disease_name | T | text |  | Condition studied, as written. | none | all |
| keywords | T | pipe_list |  | The document's own keywords. | none | all |
| Summary | T | text |  | Objective and design, verbatim from the abstract or summary. | location | all |
| study_design_as_written | T | text |  | Design sentence(s), verbatim. | location | all |
| Eligibility Criteria | T | criteria |  | Inclusion/exclusion (or case-selection) criteria in the fixed layout. One criterion per line, in the document's words, under the heading (inclusion or exclusion) the document gives it. A group of alternatives stays one line (* At least one of the following: A; B; C). A condition for one analysis only is not a study criterion. | location | all |
| Primary Outcome Measures | T | text |  | The primary outcome measure(s) the document names (ClinicalTrials.gov: Primary Outcome Measure), verbatim; empty when it names none. | location | all |
| dose_selection_rationale | T | text |  | Why these doses were chosen, verbatim. | location | all |
| rescue_or_escalation_rule | T | text |  | The rule for adding or raising treatment when the first is not enough: threshold and action, verbatim. When the topic has its own field for it, that field takes it. | location | all |
| trial_results_summary | T | text |  | The sentences reporting the result for the study's main aim (the primary endpoint when one is named), verbatim, at most 3. Reported results only: empty for a protocol or a plan. | location | all |
| Started date | T | date |  | Start of enrolment (retrospective: date of the first case in the records searched), at the precision stated. | quote | all |
| enrolment_end_date | T | date |  | End of enrolment (last animal enrolled), at the precision stated. | quote | all |
| Complete Date | T | date |  | End of the study (retrospective: date of the last case in the records searched), at the precision stated. | quote | all |
| n_enrolled | T | integer |  | Animals enrolled (retrospective: cases included). | quote | all |
| n_randomised | T | integer |  | Animals randomised. | quote | Study Type = INTERVENTIONAL |
| n_analysed | T | integer |  | Animals in the analysis of the primary endpoint; with none named, in the main analysis. | quote | all |
| n_screened | T | integer |  | Animals assessed for eligibility before any exclusion (retrospective: records found). | quote | all |
| n_excluded | T | integer |  | Animals assessed but not enrolled. | quote | all |
| n_withdrawn | T | integer |  | Animals enrolled who left the study or were removed from the analysis (withdrawn, excluded after enrolment, protocol violation). | quote | all |
| n_lost_to_follow_up | T | integer |  | Animals enrolled whose outcome is unknown because follow-up stopped. | quote | all |
| flow_as_written | T | text |  | The passage or figure legend describing how many animals were screened, excluded, enrolled, withdrawn and analysed, verbatim. | location | all |
| follow_up_as_written | T | text |  | How long and how the animals were followed (e.g. rechecks at days 7, 14, 30, 60 and 365), verbatim. | location | all |
| sample_size_as_written | T | text |  | The sample-size calculation (expected effect, power, alpha, allocation ratio), verbatim. | location | all |
| age_min_value | T | number |  | Youngest animal enrolled, as reported (the observed range; entry limits belong in Eligibility Criteria). | quote | all |
| age_min_unit | T | enum | days, weeks, months, years | Unit of age_min_value. | none | all |
| age_max_value | T | number |  | Oldest animal enrolled, as reported (the observed range; entry limits belong in Eligibility Criteria). | quote | all |
| age_max_unit | T | enum | days, weeks, months, years | Unit of age_max_value. | none | all |
| age_as_written | T | text |  | Age statement, verbatim. | location | all |
| weight_min_value | T | number |  | Lightest animal enrolled, as reported (the observed range; entry limits belong in Eligibility Criteria). | quote | all |
| weight_min_unit | T | enum | kg, lb, g | Unit of weight_min_value. | none | all |
| weight_max_value | T | number |  | Heaviest animal enrolled, as reported (the observed range; entry limits belong in Eligibility Criteria). | quote | all |
| weight_max_unit | T | enum | kg, lb, g | Unit of weight_max_value. | none | all |
| weight_as_written | T | text |  | Weight statement, verbatim. | location | all |
| species | T | pipe_list |  | Species studied, as written. | none | all |
| Sex | C | enum | ALL, MALE, FEMALE | Sex restriction. MALE or FEMALE = the document states that only that sex was eligible or enrolled; otherwise ALL. | quote | all |
| intervention_type | C | enum | DRUG, BIOLOGICAL, DEVICE, PROCEDURE, DIETARY_SUPPLEMENT, RADIATION, BEHAVIORAL, GENETIC, DIAGNOSTIC_TEST, COMBINATION_PRODUCT, OTHER | Type of the treatment the study tests. As ClinicalTrials.gov defines Intervention Type. | quote | all |
| control_type | C | enum | placebo, active, dose_comparison, no_treatment, historical, none | What the tested treatment is compared against. As ICH E10 classifies control groups; a therapy every group receives is not the control (add-on study). dose_comparison = dose-response, historical = external, none = uncontrolled. | quote | all |
| allocation | C | enum | parallel, crossover, factorial, sequential, single_group | How animals are given the treatments. As ClinicalTrials.gov defines Interventional Study Model; sequential = groups are assigned based on prior milestones being reached, as in dose escalation. | quote | Study Type = INTERVENTIONAL |
| randomised | C | enum | yes, no | Random allocation. As ClinicalTrials.gov defines Allocation: yes = Randomized, no = Nonrandomized. | quote | Study Type = INTERVENTIONAL |
| randomisation_as_written | T | text |  | How animals were allocated to arms (method, ratio, blocks or strata, concealment), verbatim. | location | Study Type = INTERVENTIONAL |
| blinding | C | enum | none, single, double, blinded_unspecified | Blinding. As ICH E9 defines open-label (none), single-blind and double-blind; blinded_unspecified = blinded, without saying who. | quote | all |
| outcome_instruments | T | pipe_list |  | Named scales or scores used to measure outcomes, as named. | none | all |
| Sponsor | T | pipe_list |  | Funders or sponsors, as written. | none | all |
| Country | C | pipe_list | see vocab/countries.txt | Countries where the animals were enrolled: as stated, or the country of the study site the document names. Names from vocab/countries.txt (the app's ClinicalTrials.gov names), sorted. | quote | all |
| trial_outcome | C | enum | positive, negative, mixed, inconclusive, unknown | Whether the main outcome met its goal. The outcome in Primary Outcome Measures against the goal the document set (better than, not worse than, a target). positive = met; negative = not met; mixed = outcomes, doses or arms disagree; inconclusive = no goal or comparison stated, or too few animals by the authors' account; unknown = no results reported. | quote | all |
| outcome_basis | C | enum | primary_endpoint, authors_conclusion_only | What trial_outcome rests on. primary_endpoint = the document calls that outcome primary; authors_conclusion_only = it does not. | quote | all |
| not_applicable | C | pipe_list | field names | Row-level exceptions: field names (studies or the study's topic fields) that do not apply to this study. List a field only when applies_to says it applies but the design rules it out for this study; give the reason in notes. | none | all |
| notes | N | text |  | Only ambiguity, or what was left empty and why. Never facts that are not in the document. | none | all |

## study_topics

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| topic | K | text |  | A topic this study belongs to: the name of a file in topics/. | none | all |

## topic_fields

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| topic | K | text |  | The topic that defines the field. | none | all |
| field | K | text |  | Field name, defined in topics/&lt;topic&gt;.toml. | none | all |
| value | T | text |  | The value. Its kind, type, allowed values and evidence come from the topic's definition. | none | all |

## arms

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| arm_id | K | text |  | &lt;ID&gt;-A&lt;n&gt;. | none | all |
| arm_label | T | text |  | Arm name as written. | none | all |
| is_control | C | enum | yes, no | Comparator arm. yes = the group the others are compared against. | quote | all |
| n_assigned | T | integer |  | Animals assigned (crossover: animals that received this arm). | quote | all |
| n_analysed | T | integer |  | Animals analysed in this arm. | quote | all |
| arm_as_written | T | text |  | The arm's description, verbatim. | location | all |

## arm_doses

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| arm_id | K | text |  | Arm key. | none | all |
| dose_id | K | text |  | &lt;arm_id&gt;-D&lt;n&gt;. | none | all |
| drug | T | text |  | Drug or treatment as written. One given to every arm is listed in each arm. | none | all |
| dose_value | T | number |  | Dose. A dose as prescribed or planned: its number (a range: the lower end). A dose reported as a summary of what the animals received (a mean ± SD, a median with its range or IQR): the mean or median, with dose_statistic saying which; the spread stays in dose_as_written. | quote | all |
| dose_value_max | T | number |  | Upper end when a range is stated. | quote | all |
| dose_statistic | C | enum | stated, mean, median | What dose_value is. stated = the dose as prescribed or planned (one number or a range); mean or median = the dose reported as a summary of what the animals received (2.0 ± 0.1 mg/kg; median 17.5 mg/kg/day). Empty when dose_value is empty. | quote | all |
| dose_unit | T | text |  | Unit as written (mg/kg, µg/kg/h, mg/m2, ...). | none | all |
| route | C | pipe_list | PO, IV, IM, SC, transdermal, topical, ophthalmic, otic, intranasal, rectal, intramammary, intrathecal, intraperitoneal, local_infiltration, epidural, intra_articular, inhaled, in_feed, in_water, other | Route. As stated; several routes for the same dose (e.g. IV or PO) as a list; other = any other route. | quote | all |
| frequency | T | text |  | As written. | none | all |
| timing | T | text |  | When the dose is given, relative to an event or within the schedule, as written (before surgery; week 3 to 4). | none | all |
| dose_duration | T | text |  | How long this dose is given, as written. A dose that changes over time is one row per step. | none | all |
| dose_as_written | T | text |  | The dose statement, verbatim. | location | all |

## results

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| result_id | K | text |  | &lt;ID&gt;-R&lt;n&gt;. | none | all |
| ID | K | text |  | Study key. | none | all |
| source_id | K | text |  | Document the result comes from. | none | all |
| arm_id | K | text |  | Arm; empty = the whole cohort. | none | all |
| result_kind | C | enum | baseline, treatment_given, outcome, adverse_event, participant_flow | What kind of number this row holds. baseline = measured at entry, before treatment or follow-up; treatment_given = how many animals received a treatment or procedure; outcome = a measure the study set out to assess, survival and death included; adverse_event = any other unfavourable, unintended event (VICH GL9: adverse event); participant_flow = how many animals were assessed, excluded, enrolled, allocated, withdrawn or analysed (CONSORT: participant flow). | none | all |
| subgroup | T | text |  | The group the row is about when it is neither one arm nor the whole study, as written; arm_id stays empty. | none | all |
| population | C | enum | intention_to_treat, modified_intention_to_treat, per_protocol, safety, other | Analysis population. Only as the document labels the analysis; empty when not stated; other = any other population, named in notes. | none | all |
| endpoint | T | text |  | What was measured, counted or predicted, as written. For one category of it, add the category (Sign: yes). In a table, take the words from the heading or title that names it. | none | all |
| timepoint_value | T | number |  | The time as a number. A label (T3, visit 3) is not a time: use the time the document gives it, else leave timepoint_value and timepoint_unit empty. | none | all |
| timepoint_unit | C | enum | minutes, hours, days, weeks, months, years | Unit of timepoint_value. As reported. | none | all |
| timepoint_as_written | T | text |  | The time with what it is counted from, verbatim (24 h before the visit; day 28 of treatment). | none | all |
| reference_timepoint_as_written | T | text |  | For a change within the same animals: the earlier time compared against, verbatim (before treatment). | none | all |
| n | T | integer |  | Denominator. | none | all |
| events | T | integer |  | Numerator (the 12 in 12/20). | none | all |
| statistic_type | C | enum | proportion, count, mean, median, mean_difference, median_difference, percent_change, odds_ratio, risk_ratio, risk_difference, hazard_ratio, median_time_to_event, survival_probability, rate, correlation, regression_coefficient, sensitivity, specificity, other | Statistic. As reported; other = any other statistic, named in notes. | none | all |
| value | T | number |  | Number as reported (60 for 60%). | none | all |
| value_unit | T | text |  | %, days, score points, ... | none | all |
| variability_type | C | enum | SD, SE, CI95, CI90, IQR, range, other | Spread. As reported. | none | all |
| variability_value | T | number |  | For SD or SE. | none | all |
| variability_low | T | number |  | Lower bound (CI, IQR, range). | none | all |
| variability_high | T | number |  | Upper bound. | none | all |
| comparison | T | text |  | The two things compared, as written (A vs B). For a ratio, a difference or a coefficient tied to a factor: the factor's name, then its levels or unit (Factor: level A vs level B). Empty when the number compares nothing. | none | all |
| reference_arm_id | K | text |  | Arm compared against, when stated; empty when the comparison is against pooled arms (name them in comparison). | none | all |
| p_value | T | text |  | The p-value the article prints for this result's statistical test, as written, keeping &lt; (e.g. &lt;0.05). Empty when it prints none; any other statistic goes in value. | none | all |
| statistical_test | T | text |  | Test or model as written (e.g. 'log-rank test', 'Cox regression'). | none | all |
| ae_attributed_to | C | enum | treatment, disease, procedure, unclear | Adverse events: cause as attributed by the authors. What the authors attribute the event to; unclear = they say it is uncertain; empty = not stated. | none | result_kind = adverse_event |
| ae_grade | T | text |  | Adverse events: severity or grade as written (e.g. 'VCOG-CTCAE grade 2', 'major'). | none | result_kind = adverse_event |
| ae_fatal | C | enum | died, euthanised, no | Adverse events: whether the event ended in death. died or euthanised = the row's passage (as_written) says so (died also when it doesn't separate the two); no = it says the animal survived or the event was not fatal; empty otherwise. | none | result_kind = adverse_event |
| as_written | T | text |  | The verbatim sentence or table row (this is the quote). | none | all |
| location | T | text |  | Section / table / page. | none | all |
| notes | N | text |  | Only ambiguity, or what a coded field could not say (e.g. which population 'other' is). | none | all |

## sites

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| site_no | K | integer |  | Unique within a study. | none | all |
| Facility | T | text |  | Site name as written. | none | all |
| City | T | text |  | As written. | none | all |
| Country | C | enum | see vocab/countries.txt | Country of the site: as stated, or the country of the named institution. Name from vocab/countries.txt. | quote | all |

## people

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| source_id | K | text |  | Document that lists this person. | none | all |
| person_no | K | integer |  | Unique within a study. | none | all |
| Name | T | text |  | As written. | none | all |
| Affiliation | T | text |  | As written. | none | all |
| role | C | enum | author, investigator, study_director | Role. author = listed as an author; investigator = named as a study investigator, not an author; study_director = named as study director. | quote | all |
| author_position | C | enum | first, middle, last | Position in the author list. Authors only. | quote | all |
| is_corresponding | C | enum | yes, no | Corresponding author. yes = marked as the corresponding author. | quote | all |
| site_no | K | integer |  | Only when the document links this person to a site. Never pair people and sites by assumption. | none | all |

## breeds

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| arm_id | K | text |  | Arm; empty = the whole study. | none | all |
| Breed | T | text |  | As written (including 'mixed breed'). | none | all |
| n_dogs | T | integer |  | Number of animals of this breed. | quote | all |
| percent | T | number |  | Share of animals of this breed, in %, only when the count is not given. | quote | all |

## definitions

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| source_id | K | text |  | Document the definition comes from. | none | all |
| ID | K | text |  | Study key; empty for consensus statements and guidelines. | none | all |
| term | T | text |  | e.g. complete remission, case definition, relapse. | none | all |
| definition_verbatim | T | text |  | The definition, verbatim. | none | all |
| location | T | text |  | Section / table / page. | none | all |

## conflicts

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| table | T | enum | studies, arms, arm_doses, results, sites, people, breeds, definitions, topic_fields | Table of the disputed value. | none | all |
| row_key | K | text |  | The row as in evidence.row_key; a short description when the conflict is not about one row. | none | all |
| field | T | text |  | Column of the disputed value. | none | all |
| primary_source_id | K | text |  | Document whose value the table keeps. | none | all |
| primary_value | T | text |  | The value the table keeps. | none | all |
| secondary_source_id | K | text |  | Document giving the other value (the same document when it contradicts itself). | none | all |
| secondary_value | T | text |  | The other value, as written. | none | all |
| resolution | C | enum | primary_wins, corrected, unresolved | How the conflict was settled. primary_wins = the primary document's value is kept (the default); corrected = a person chose the value; unresolved = still to decide. | none | all |
| note | N | text |  | Where each value is stated, in your own words. | none | all |

## study_links

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key: the lower VET number of the two. | none | all |
| linked_ID | K | text |  | The other study's key. | none | all |
| relation | C | enum | same_experiment, shared_animals, distinct | How the two studies relate. same_experiment = both rows report the same animals in the same experiment (the same groups and treatments), e.g. a paper and a sub-study of its samples, or a later report with longer follow-up: count them once; shared_animals = some of the same animals, in another experiment or cohort (a research colony used again, an overlapping enrolment); distinct = checked: separate studies that only look alike. same_experiment and shared_animals need a document's own words saying so, in statement_verbatim. | none | all |
| source_id | K | text |  | The document whose words show the relation (a document of one of the two studies); empty for distinct. | none | all |
| statement_verbatim | T | text |  | The document's words, verbatim (e.g. Blood samples … were collected … during a related study …). | none | all |
| location | T | text |  | Section / table / page of the words. | none | all |
| decided_by | C | enum | vet, curator | Who decided. vet = a veterinarian decided; curator = whoever builds the database decided (for the proof of concept, Claude, logged in decisions.md). | none | all |
| note | N | text |  | Why, in your own words; for distinct, what differs. | none | all |

## evidence

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| source_id | K | text |  | Document the quote comes from. | none | all |
| table | T | enum | studies, arms, arm_doses, sites, people, breeds, topic_fields | Table of the supported value. | none | all |
| row_key | K | text |  | arm_id \| dose_id \| site_no \| person_no \| &lt;arm_id&gt;\|&lt;Breed&gt; \| the topic for topic_fields; empty for studies. | none | all |
| field | T | text |  | Column name of the supported value. | none | all |
| value | T | text |  | Exactly as entered in that field. | none | all |
| quote | T | text |  | Verbatim support, at most 500 characters; empty for location-only rows. | none | all |
| quote_truncated | C | enum | yes, no | Quote cut to 500 characters. yes when the quote was cut. | none | all |
| location | T | text |  | Section / table / page. | none | all |

## derived

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| icd10_primary | D | text |  | Computed downstream by our code. Never extracted. ICD-10 proxy code. | none | all |
| icd10_alternates | D | text |  | Computed downstream by our code. Never extracted. Alternate ICD-10 codes. | none | all |
| disease_stage | D | text |  | Computed downstream by our code. Never extracted. Disease stage. | none | all |
| main_category | D | text |  | Computed downstream by our code. Never extracted. Disease category. | none | all |
| Continent | D | text |  | Computed downstream by our code. Never extracted. From Country. | none | all |
| Year | D | text |  | Computed downstream by our code. Never extracted. From Started date. | none | all |
| Duration (days) | D | text |  | Computed downstream by our code. Never extracted. From the dates. | none | all |
| Duration (months) | D | text |  | Computed downstream by our code. Never extracted. From the dates. | none | all |
| Trial_E | D | text |  | Computed downstream by our code. Never extracted. Enrolment measure used by the app. | none | all |
| enrolment_rate | D | text |  | Computed downstream by our code. Never extracted. Animals per month. | none | all |
| unit_conversions | D | text |  | Computed downstream by our code. Never extracted. lb to kg, months to years, and similar. | none | all |
| percentages | D | text |  | Computed downstream by our code. Never extracted. Percentages computed from counts. | none | all |
| pooled_values | D | text |  | Computed downstream by our code. Never extracted. Pooled arms. | none | all |
| Name_Affiliation_Sponsor | D | text |  | Computed downstream by our code. Never extracted. Combined field used by the app. | none | all |
| KOL_list | D | text |  | Computed downstream by our code. Never extracted. Built from people. | none | all |
| embeddings | D | text |  | Computed downstream by our code. Never extracted. Vector embeddings. | none | all |

## topic: cie

Stored in `topic_fields` with topic = `cie`.

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ce_subtype | C | enum | FRE, ARE, IRE, NRE, mixed, unclassified | Chronic enteropathy subtype by treatment response. FRE food-, ARE antibiotic-, IRE immunosuppressant-responsive, NRE non-responsive; mixed = several; unclassified = not classified. | quote | all |
| glucocorticoid_protocol | T | text |  | Starting dose and taper, verbatim. | location | all |
| concurrent_diet | T | text |  | Diet given alongside glucocorticoids, verbatim. | location | all |
| prior_diet_trial | T | text |  | Diet trial before entry, verbatim. | location | all |
| prior_antibiotic_trial | T | text |  | Antibiotic trial before entry, verbatim. | location | all |
| user_type | C | enum | new_users, prevalent_users, mixed, unclear | Treatment status at entry. new_users = enter at the start of glucocorticoid treatment; prevalent_users = already on treatment at entry. | quote | all |

## topic: imha

Stored in `topic_fields` with topic = `imha`.

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| imha_classification | C | enum | non_associative, associative, mixed, unspecified | IMHA classification. non_associative = primary/idiopathic; associative = secondary to a stated cause. | quote | all |
| immunosuppressive_protocol | T | text |  | Immunosuppressive protocol, verbatim. | location | all |
| antithrombotic_protocol | T | text |  | Antithrombotic protocol, verbatim. | location | all |
| transfusion_trigger | T | text |  | What triggers a transfusion, verbatim. | location | all |
| entry_pcv | T | text |  | PCV (or haematocrit, saying which) at entry, verbatim. | location | all |

## topic: surgical_pain

Stored in `topic_fields` with topic = `surgical_pain`.

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| surgery_types | T | pipe_list |  | Surgeries as written. | none | all |
| surgery_category | C | enum | soft_tissue, orthopedic, dental, neurologic, ophthalmic, mixed | Surgery category. mixed = more than one category in the study. | quote | all |
| anesthesia_protocol | T | text |  | Anaesthesia protocol, verbatim. | location | all |
| premedication | T | text |  | Premedication, verbatim. | location | all |
| pain_assessment_times | T | text |  | When pain was scored, verbatim. | location | all |
| rescue_threshold | T | text |  | Score that triggers rescue analgesia, verbatim. | location | all |
| multimodal_cointerventions | T | text |  | Analgesics given to all arms alongside the test treatment, verbatim. | location | all |
