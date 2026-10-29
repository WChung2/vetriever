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
| study_kind | C | enum | prospective_trial, retrospective_cohort, case_series, other | Kind of study. prospective_trial = dogs enrolled and followed forward under a protocol (including regulatory studies); retrospective_cohort = review of existing records of a defined group; case_series = descriptive report of several cases with no comparison; other = none of these. | quote | all |
| study_stage | C | enum | pilot, dose_determination, dose_confirmation, field_effectiveness, safety, pharmacokinetic, other | Development stage. pilot = called pilot, preliminary or feasibility; dose_determination = compares two or more doses of the same treatment to find the effective dose; dose_confirmation = confirms a chosen dose against a control; field_effectiveness = efficacy in clinical patients at fixed doses; safety = main aim is safety or tolerance; pharmacokinetic = main aim is drug levels; other = none of these. | quote | study_kind = prospective_trial or other |
| Study Type | C | enum | INTERVENTIONAL, OBSERVATIONAL | App study type. INTERVENTIONAL = treatment assigned by the study protocol; OBSERVATIONAL = treatment given in routine care, retrospective review, cohort. | quote | all |
| Overall Status | C | enum | COMPLETED, TERMINATED, RECRUITING, ACTIVE_NOT_RECRUITING, UNKNOWN | Study status. COMPLETED when results are reported and no early stop is stated; TERMINATED when an early stop is stated; RECRUITING or ACTIVE_NOT_RECRUITING only as stated by a registry entry; otherwise UNKNOWN. | quote | all |
| disease_name | T | text |  | Condition studied, as written. | none | all |
| keywords | T | pipe_list |  | The document's own keywords. | none | all |
| Summary | T | text |  | Objective and design, verbatim from the abstract or summary. | location | all |
| study_design_as_written | T | text |  | Design sentence(s), verbatim. | location | all |
| Eligibility Criteria | T | criteria |  | Inclusion/exclusion (or case-selection) criteria in the fixed layout. | location | all |
| Primary Outcome Measures | T | text |  | Primary endpoint(s), verbatim. | location | all |
| dose_selection_rationale | T | text |  | Why these doses were chosen, verbatim. | location | all |
| rescue_or_escalation_rule | T | text |  | Rescue analgesia (pain), transfusion trigger (IMHA) or adding an immunosuppressant (CIE): threshold and action, verbatim. | location | all |
| trial_results_summary | T | text |  | 1-3 sentences verbatim from results or conclusion. | location | all |
| Started date | T | date |  | Start of enrolment, at the precision stated. | quote | all |
| Complete Date | T | date |  | End of the study, at the precision stated. | quote | all |
| n_enrolled | T | integer |  | Dogs enrolled (retrospective: cases included). | quote | all |
| n_randomised | T | integer |  | Dogs randomised. | quote | study_kind = prospective_trial or other |
| n_analysed | T | integer |  | Denominator of the primary endpoint. | quote | all |
| age_min_value | T | number |  | Minimum age. | quote | all |
| age_min_unit | T | enum | days, weeks, months, years | Unit of age_min_value. | none | all |
| age_max_value | T | number |  | Maximum age. | quote | all |
| age_max_unit | T | enum | days, weeks, months, years | Unit of age_max_value. | none | all |
| age_as_written | T | text |  | Age statement, verbatim. | location | all |
| weight_min_value | T | number |  | Minimum body weight. | quote | all |
| weight_min_unit | T | enum | kg, lb, g | Unit of weight_min_value. | none | all |
| weight_max_value | T | number |  | Maximum body weight. | quote | all |
| weight_max_unit | T | enum | kg, lb, g | Unit of weight_max_value. | none | all |
| weight_as_written | T | text |  | Weight statement, verbatim. | location | all |
| species | T | pipe_list |  | Species studied, as written. | none | all |
| Sex | C | enum | ALL, MALE, FEMALE | Sex restriction. MALE or FEMALE only when eligibility or the enrolled dogs are restricted to one sex (a female-only surgery such as ovariohysterectomy counts as stated); otherwise ALL. | quote | all |
| intervention_type | C | enum | DRUG, BIOLOGICAL, DEVICE, PROCEDURE, DIETARY_SUPPLEMENT, OTHER | Type of the treatment under study. BIOLOGICAL = vaccine, blood product or cell therapy; PROCEDURE = surgical or anaesthetic technique; therapeutic diet = OTHER. | quote | all |
| control_type | C | enum | placebo, active, dose_comparison, no_treatment, historical, none | Comparator. placebo = inactive treatment; active = another active treatment; dose_comparison = other doses of the same treatment only; no_treatment = untreated group; historical = earlier cases or literature; none = no control group. | quote | all |
| allocation | C | enum | parallel, crossover, single_group | How dogs receive arms. parallel = each dog gets one arm; crossover = each dog gets several arms in sequence; single_group = one arm only. | quote | all |
| randomised | C | enum | yes, no | Random allocation. yes only when random allocation is stated. | quote | study_kind = prospective_trial or other |
| blinding | C | enum | none, single, double, blinded_unspecified | Blinding. none = open label; single or double as stated; blinded_unspecified = blinded but who is not stated. | quote | study_kind = prospective_trial or other |
| outcome_instruments | T | pipe_list |  | Scales or scores used, as named (e.g. CMPS-SF\|VAS). | none | all |
| Sponsor | T | pipe_list |  | Funders or sponsors, as written. | none | all |
| Country | C | pipe_list | see vocab/countries.txt | Countries where dogs were enrolled. Names from vocab/countries.txt (the app's ClinicalTrials.gov names), sorted. | quote | all |
| trial_outcome | C | enum | positive, negative, mixed, inconclusive, unknown | Result of the primary endpoint. Based on the pre-specified primary endpoint as reported, not the authors' framing. positive = primary endpoint met in favour of the treatment; negative = not met; mixed = primary endpoints or doses/arms disagree; inconclusive = results reported but no formal comparison, or authors state the study was underpowered; unknown = no results in any linked document. Quote the sentence reporting the primary-endpoint result. | quote | study_kind = prospective_trial or other |
| outcome_basis | C | enum | primary_endpoint, authors_conclusion_only | What trial_outcome rests on. authors_conclusion_only when the document gives no primary-endpoint result. | quote | study_kind = prospective_trial or other |
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
| is_control | C | enum | yes, no | Comparator arm. yes = placebo, active-control or no-treatment arm. | quote | all |
| n_assigned | T | integer |  | Dogs assigned (crossover: dogs that received this arm). | quote | all |
| n_analysed | T | integer |  | Dogs analysed in this arm. | quote | all |
| arm_as_written | T | text |  | The arm's description, verbatim. | location | all |

## arm_doses

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| arm_id | K | text |  | Arm key. | none | all |
| dose_id | K | text |  | &lt;arm_id&gt;-D&lt;n&gt;. | none | all |
| drug | T | text |  | Drug as written. | none | all |
| dose_value | T | number |  | Dose (lower end of a range). | quote | all |
| dose_value_max | T | number |  | Upper end when a range is stated. | quote | all |
| dose_unit | T | text |  | Unit as written (mg/kg, µg/kg/h, mg/m2, ...). | none | all |
| route | C | enum | PO, IV, IM, SC, transdermal, topical, local_infiltration, epidural, intra_articular, inhaled, other | Route. As stated; other = any other route. | quote | all |
| frequency | T | text |  | As written. | none | all |
| timing | T | text |  | When given, as written (e.g. 30 min before induction). | none | all |
| dose_duration | T | text |  | How long, as written, including any taper. | none | all |
| dose_as_written | T | text |  | The dose statement, verbatim. | location | all |

## results

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| result_id | K | text |  | &lt;ID&gt;-R&lt;n&gt;. | none | all |
| ID | K | text |  | Study key. | none | all |
| source_id | K | text |  | Document the result comes from. | none | all |
| arm_id | K | text |  | Arm; empty = the whole cohort. | none | all |
| endpoint | T | text |  | Endpoint as written. | none | all |
| timepoint_value | T | number |  | Timepoint. | none | all |
| timepoint_unit | C | enum | minutes, hours, days, weeks, months, years | Unit of timepoint_value. As reported. | none | all |
| timepoint_as_written | T | text |  | Timepoint, verbatim. | none | all |
| n | T | integer |  | Denominator. | none | all |
| events | T | integer |  | Numerator (the 12 in 12/20). | none | all |
| statistic_type | C | enum | proportion, count, mean, median, mean_difference, median_difference, odds_ratio, risk_ratio, hazard_ratio, median_time_to_event, other | Statistic. As reported. | none | all |
| value | T | number |  | Number as reported (60 for 60%). | none | all |
| value_unit | T | text |  | %, days, score points, ... | none | all |
| variability_type | C | enum | SD, SE, CI95, CI90, IQR, range, other | Spread. As reported. | none | all |
| variability_value | T | number |  | For SD or SE. | none | all |
| variability_low | T | number |  | Lower bound (CI, IQR, range). | none | all |
| variability_high | T | number |  | Upper bound. | none | all |
| comparison | T | text |  | As written (e.g. high dose vs placebo). | none | all |
| reference_arm_id | K | text |  | Arm compared against, when stated. | none | all |
| p_value | T | text |  | As written, keeping &lt; (e.g. &lt;0.05). | none | all |
| as_written | T | text |  | The verbatim sentence or table row (this is the quote). | none | all |
| location | T | text |  | Section / table / page. | none | all |

## sites

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| ID | K | text |  | Study key. | none | all |
| site_no | K | integer |  | Unique within a study. | none | all |
| Facility | T | text |  | Site name as written. | none | all |
| City | T | text |  | As written. | none | all |
| Country | C | enum | see vocab/countries.txt | Country of the site. Name from vocab/countries.txt. | quote | all |

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
| n_dogs | T | integer |  | Number of dogs of this breed. | quote | all |

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
| enrolment_rate | D | text |  | Computed downstream by our code. Never extracted. Dogs per month. | none | all |
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
| follow_up_as_written | T | text |  | Follow-up, verbatim. | location | all |
| user_type | C | enum | new_users, prevalent_users, mixed, unclear | Treatment status at entry. new_users = enter at the start of glucocorticoid treatment; prevalent_users = already on treatment at entry. | quote | all |

## topic: imha

Stored in `topic_fields` with topic = `imha`.

| field | kind | type | allowed values | meaning and rule | evidence | applies to |
|---|---|---|---|---|---|---|
| imha_classification | C | enum | non_associative, associative, mixed, unspecified | IMHA classification. non_associative = primary/idiopathic; associative = secondary to a stated cause. | quote | all |
| immunosuppressive_protocol | T | text |  | Immunosuppressive protocol, verbatim. | location | all |
| antithrombotic_protocol | T | text |  | Antithrombotic protocol, verbatim. | location | all |
| transfusion_trigger | T | text |  | What triggers a transfusion, verbatim. | location | all |
| entry_pcv | T | text |  | PCV at entry, verbatim. | location | all |

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
