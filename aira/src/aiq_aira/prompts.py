# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

query_writer_instructions="""Generate {number_of_queries} search queries that will help with planning the sections of the final report.

# Report topic
{topic}

# Report organization
{report_organization}

# Instructions
1. Create queries to help answer questions for all sections in report organization.
2. Format your response as a JSON object with the following keys:
- "query": The actual search query string
- "report_section": The section of report organization the query is generated for
- "rationale": Brief explanation of why this query is relevant to report organization

**Output example**
```json
[
    {{
        "query": "What is a transformer?",
        "report_section": "Introduction",
        "rationale": "Introduces the user to transformer"
    }},
    {{
        "query": "machine learning transformer architecture explained",
        "report_section": "technical architecture",
        "rationale": "Understanding the fundamental structure of transformer models"
    }}
]
```"""

summarizer_instructions="""Generate a high-quality report from the given sources. 

# Report organization
{report_organization}

# Knowledge Sources
{source}

# Instructions
1. Stick to the sections outlined in report organization
2. Highlight the most relevant pieces of information across all sources
3. Provide a concise and comprehensive overview of the key points related to the report topic
4. Focus the bulk of the analysis on the most significant findings or insights
5. Ensure a coherent flow of information
6. You should use proper markdown syntax when appropriate, as the text you generate will be rendered in markdown. Do NOT wrap the report in markdown blocks (e.g triple backticks).
7. Start report with a title
8. Do not include any source citations, as these will be added to the report in post processing.
"""


report_extender = """Add to the existing report additional sources preserving the current report structure (sections, headings etc).

# Draft Report
{report}

# New Knowledge Sources
{source}

# Instructions
1. Copy the original report title
2. Preserve the report structure (sections, headings etc)
3. Seamlessly add information from the new sources.
4. Do not include any source citations, as these will be added to the report in post processing.
"""


reflection_instructions = """Using report organization as a guide identify knowledge gaps and/or areas that have not been addressed comprehensively in the report.

# Report topic
{topic}

# Report organization
{report_organization}

# Draft Report
{report}

# Instructions
1. Focus on details that are necessary to understanding the key concepts as a whole that have not been fully covered
2. Ensure the follow-up question is self-contained and includes necessary context for web search.
3. Format your response as a JSON object with the following keys:
- query: Write a specific follow up question to address this gap
- report_section: The section of report the query is for
- rationale: Describe what information is missing or needs clarification

**Output example**
```json
{{
    "query": "What are typical performance benchmarks and metrics used to evaluate [specific technology]?"
    "report_section": "Deep dive",
    "rationale": "The report lacks information about performance metrics and benchmarks"
}}
```"""


relevancy_checker = """Determine if the Context contains proper information to answer the Question.

# Question
{query}

# Context
{document}

# Instructions
1. Give a binary score 'yes' or 'no' to indicate whether the context is able to answer the question.

**Output example**
```json
{{
    "score": "yes"
}}
```"""

finalize_report = """

Given the report draft below, format a final report according to the report structure. 

You are to format the report draft only, do not edit down / shorten the report draft. Do not omit content from the report draft. Keep the content of each section the same as before when formatting the final report. 

Do not add a sources section, sources are added in post processing. 

You should use proper markdown syntax when appropriate, as the text you generate will be rendered in markdown. Do NOT wrap the report in markdown blocks (e.g triple backticks).

Return only the final report without any other commentary or justification.

<REPORT DRAFT>
{report}
</REPORT DRAFT>

<REPORT STRUCTURE>
{report_organization}
</REPORT STRUCTURE>
"""

check_whether_virtual_screening = """Using report topic and report organization as a guide identify whether there is intention to do virtual screening. 
Virtual screening is a computational technique used in drug discovery to identify potential drug candidates from large libraries of molecules.

# Report topic
{topic}

# Report organization
{report_organization}


# Instructions
1. From the report topic and report organization, determine if virtual screening would be helpful for what the user wants to research.
2. If the report topic is not a disease or medical condition, then the intention to do virtual screening would be 'no'.
3. If the report topic is a disease or medical condition, such as cystic fibrosis, and the report organization contains mentions of proposing new/novel small molecule therapies, mentions of intentions to do virtual screening, or mentions of the target protein and a recent or novel small molecule therapy, then the intention to do virtual screening would be 'yes'.
4. Output a binary intention 'yes' or 'no' to indicate whether the virtual screening is intended.

**Output example**
```json
{{
    "intention": "yes"
}}
```"""

check_protein_molecule_found = """Using the current knowledge sources to identify whether the two ingredients needed for virtual screening are found already.
The two ingredients are: target protein related to the condition or disease, and a recent small molecule therapy for the condition or disease.
If either ingredients is missing, write a follow-up question for the missing ingredient(s).

# Report topic
{topic}

# Knowledge Sources
{knowledge_sources}


# Instructions
1. Focus on whether both of the two ingredients had already been found in the knowledge sources.
2. Ensure the follow-up question is self-contained and includes necessary context for web search.
3. If both of the ingredients are found in the current knowledge base, return the target protein and recent small molecule therapy. If the recent small molecule therapy is a combination of multiple molecules, pick only one molecule. For example, if the recent small molecule therapy is "A combination of Elexacaftor, Tezacaftor, and Ivacaftor", pick any one of the three molecules in the combination, and only return one molecule. Make sure you return a valid molecule name, and not a branded name for therapy that may contain multiple molecules such as Alyftrek. Format your response as a JSON object with the following keys:
- target_protein: the target protein for the disease or condition, be as succinct as possible, one word is ideal
- recent_small_molecule_therapy: a recent small molecule therapy that has been found in research, one molecule name only, be as succinct as possible, one word is ideal

**Output example**
```json
{{
    "target_protein": "CFTR [or another protein]",
    "recent_small_molecule_therapy": "Ivacaftor [or another small molecule therapy]"
}}
```
4. If at least one ingredient is missing, return a query on one ingredient, format your response as a JSON object with the following keys:
- query: Write a specific follow up question to identify the missing ingredient (target protein or recent small molecule therapy)
- rationale: Describe what information is missing or needs clarification

**Output example**
```json
{{
    "query": "What is the target protein related to [specific condition or disease]?",
    "report_section": "Virtual Screening Details",
    "rationale": "The knowledge sources lack information about the target protein"
}}
```
"""

combine_virtual_screening_info_into_report_prompt = """
Given the intended report structure and existing report draft below, add one additional section exactly titled "Running Virtual Screening for Novel Small Molecule Therapies" into the intended report structure, before the Conclusions section. 
Make sure to preserve the existing report draft and its exsiting format including sections, headings etc. Do not delete any content or sections from the existing report draft.

# report structure
{report_organization}

# report draft
{report}

# Virtual Screening Related Queries 
{vs_queries}
{vs_queries_results}

# Virtual Screening Process and Output Information
{vs_info}


# Instructions
1. Preserve and copy over the original report draft and structure exactly as they are. Do not delete any content or sections from the existing report draft.
2. Add one additional self-contained section "Running Virtual Screening for Novel Small Molecule Therapies" into the existing report structure. The additional section should reflect the virtual screening process for proposing novel small-molecule therapies. In this section, first give a summary of Virtual Screening Related Queries from the provided context. Then copy over the provided Virtual Screening Process and Output Information exactly as-is, without deleting any content including lists, numbers, confidence scores, success, directory name. Do not remove or simplify any content from the source Virtual Screening Process and Output Information, copy over the content word-for-word.
3. Do not include information from Virtual Screening Related Queries or Virtual Screening Process and Output Information in other sections. These info should be self-contained in "Running Virtual Screening for Novel Small Molecule Therapies" section.
4. You should use proper markdown syntax when appropriate, as the text you generate will be rendered in markdown. Do NOT wrap the report in markdown blocks (e.g triple backticks).
5. Do not include any source citations, as these will be added to the report in post processing.
"""