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

import asyncio
import aiohttp
import json
import os
import logging
import xml.etree.ElementTree as ET
from typing import List
import re
import requests
import datetime
import csv
import pubchempy as pcp
from rcsbapi.search import TextQuery, AttributeQuery
from langchain_core.runnables import RunnableConfig
from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_core.utils.json import parse_json_markdown
from langchain_core.stores import InMemoryByteStore
from langgraph.types import StreamWriter
from aiq_aira.schema import  GeneratedQuery

from aiq_aira.schema import AIRAState
from aiq_aira.prompts import (
    finalize_report,
    query_writer_instructions,
    reflection_instructions,
    check_whether_virtual_screening,
    check_protein_molecule_found,
    combine_virtual_screening_info_into_report_prompt
)

from aiq_aira.utils import async_gen, format_sources, update_system_prompt
from aiq_aira.constants import ASYNC_TIMEOUT

from aiq_aira.search_utils import process_single_query, deduplicate_and_format_sources
from aiq_aira.report_gen_utils import summarize_report

logger = logging.getLogger(__name__)
store = InMemoryByteStore()

async def generate_query(state: AIRAState, config: RunnableConfig, writer: StreamWriter):
    """
    Node for generating a research plan as a list of queries. 
    Takes in a topic and desired report organization. 
    Returns the list of query objects. 
    """
    logger.info("GENERATE QUERY")
    writer({"generating_questions": "\n Generating queries \n"}) # send something to initialize the UI so the timeout shows

    # Generate a query
    llm = config["configurable"].get("llm")
    number_of_queries = config["configurable"].get("number_of_queries")
    report_organization = config["configurable"].get("report_organization")
    topic = config["configurable"].get("topic")

    system_prompt = ""
    system_prompt = update_system_prompt(system_prompt, llm)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system", system_prompt
            ),
            (
                "human", "{input}"
            ),
        ]
    )
    chain = prompt | llm

    input = {
        "topic": topic,
        "report_organization": report_organization,
        "number_of_queries": number_of_queries,
        "input": query_writer_instructions.format(topic=topic, report_organization=report_organization, number_of_queries=number_of_queries)
    }

    answer_agg = ""
    stop = False

    try: 
        async with asyncio.timeout(ASYNC_TIMEOUT):
            async for chunk in chain.astream(input, stream_usage=True):
                answer_agg += chunk.content
                if "</think>" in chunk.content:
                    stop = True
                if not stop:
                    writer({"generating_questions": chunk.content})
    except asyncio.TimeoutError as e: 
        writer({"generating_questions": " \n \n ---------------- \n \n Timeout error from reasoning LLM, please try again"})
        queries = []
        return {"queries": queries}

    # Split to get the final JSON after </think>
    splitted = answer_agg.split("</think>")
    if len(splitted) < 2:
        writer({"generating_questions": " \n \n ---------------- \n \n Timeout error from reasoning LLM, please try again"})
        logger.info(f"Error processing query response. No </think> tag. Response: {answer_agg}")
        queries = []
        return {"queries": queries}

    json_str = splitted[1].strip()
    try:
        queries = parse_json_markdown(json_str)
    except Exception as e:
        logger.error(f"Error parsing queries as JSON: {e}")
        queries = []

    return {"queries": queries}


async def web_research(
        state: AIRAState,
        config: RunnableConfig,
        writer: StreamWriter
):
    """
    Node for performing research based on the queries returned by generate_query.
    Research is performed deterministically by running RAG (and optionally a web search) on each query.
    The function extracts the queries from the state, processes each one via process_single_query,
    and finally formats the sources into an aggregated XML structure.
    A separate list of source citations is also maintained, tracking the query, answer, and sources for each query.
    """

    logger.info("STARTING WEB RESEARCH")
    llm = config["configurable"].get("llm")
    search_web = config["configurable"].get("search_web")
    collection = config["configurable"].get("collection")

    # Determine the queries and state queries based on the type of state.
    # If the state is a list of queries, use them directly.
    queries = [q.query for q in state.queries]
    state_queries = state.queries
   

    # Process each query concurrently.
    results = await asyncio.gather(*[
        process_single_query(query, config, writer, collection, llm, search_web)
        for query in queries
    ])

    # Unpack results.
    generated_answers = [result[0] for result in results]
    citations = [result[1] if result[1] is not None else "" for result in results]
    relevancy_list = [result[2] for result in results]
    web_results = [result[3] for result in results]
    citations_web = [result[4] if result[4] is not None else "" for result in results]

    # Format the sources (producing a combined XML <sources> structure).
    search_str = deduplicate_and_format_sources(
        citations, generated_answers, relevancy_list, web_results, state_queries
    )

    all_citations = []
    for idx, citation in enumerate(citations):
        if relevancy_list[idx]["score"] == "yes":
            all_citations.append(citation)
        if relevancy_list[idx]["score"] != "yes" and citations_web[idx] not in ["N/A", ""]:
            all_citations.append(citations_web[idx])

    all_citations = set(all_citations) # remove duplicates
    citation_str = "\n".join(all_citations)
    return {"citations": citation_str, "web_research_results": [search_str]}


async def summarize_sources(
        state: AIRAState,
        config: RunnableConfig,
        writer: StreamWriter
):
    """
    Node for summarizing or extending an existing summary. Takes the web research report and writes a report draft.
    """
    logger.info("SUMMARIZE")
    llm = config["configurable"].get("llm")
    report_organization = config["configurable"].get("report_organization")

    # The most recent web research
    most_recent_web_research = state.web_research_results[-1]
    existing_summary = state.running_summary

    # -- Call the helper function here --
    updated_report = await summarize_report(
        existing_summary=existing_summary,
        new_source=most_recent_web_research,
        report_organization=report_organization,
        llm=llm,
        writer=writer
    )

    state.running_summary = updated_report

    writer({"running_summary": updated_report})
    return {"running_summary": updated_report}


async def reflect_on_summary(state: AIRAState, config: RunnableConfig, writer: StreamWriter):
    """
    Node for reflecting on the summary to find knowledge gaps. 
    Identified gaps are added as new queries.
    Number of new queries is determined by the num_reflections parameter.
    For each new query, the node performs web research and report extension.
    The extended report and additional citations are added to the state.
    """
    logger.info("REFLECTING")
    llm = config["configurable"].get("llm")
    num_reflections = config["configurable"].get("num_reflections")
    report_organization = config["configurable"].get("report_organization")
    search_web = config["configurable"].get("search_web")
    collection = config["configurable"].get("collection")

    logger.info(f"REFLECTING {num_reflections} TIMES")

    for i in range(num_reflections):
        input = {
            "input": reflection_instructions.format(report_organization=report_organization, topic=config["configurable"].get("topic"), report=state.running_summary)

        }
        system_prompt = ""
        system_prompt = update_system_prompt(system_prompt, llm)

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system", system_prompt
                ),
                (
                    "human", "Using report organization as a guide identify a knowledge gap and generate a follow-up web search query based on our existing knowledge. \n \n {input}"
                ),
            ]
        )
        chain = prompt | llm

        writer({"reflect_on_summary": "\n Starting reflection \n"})
        async for i in async_gen(1):
            result = ""
            stop = False
            async for chunk in chain.astream(input, stream_usage=True):
                result = result + chunk.content
                if chunk.content == "</think>":
                    stop = True
                if not stop:
                    writer({"reflect_on_summary": chunk.content})

        splitted = result.split("</think>")
        if len(splitted) < 2:
            # If we can't parse anything, just fallback
            running_summary = state.running_summary
            writer({"running_summary": running_summary})
            return {"running_summary": running_summary}

        reflection_json = splitted[1].strip()
        try:
            reflection_obj = parse_json_markdown(reflection_json)
            gen_query = GeneratedQuery(
                query=reflection_obj["query"] if "query" in reflection_obj else str(reflection_obj),
                report_section="All",
                rationale="Reflection-based query"
            )
        except Exception as e:
            logger.warning(f"Error parsing reflection JSON: {e}")
            reflection_obj = reflection_json
            gen_query = GeneratedQuery(
                query=reflection_obj,
                report_section="All",
                rationale="Reflection-based query"
            )


        rag_answer, rag_citation, relevancy, web_answer, web_citation = await process_single_query(
            query=gen_query.query,
            config=config,
            writer=writer,
            collection=collection,
            llm=llm,
            search_web=search_web
        )


        search_str = deduplicate_and_format_sources(
            [rag_citation], [rag_answer], [relevancy], [web_answer], [gen_query]
        )

        state.web_research_results.append(search_str)
        
        if relevancy['score'] == "yes" and rag_citation is not None:
            state.citations = "\n".join([state.citations, rag_citation])

        if relevancy['score'] != "yes" and web_citation not in ["N/A", ""] and web_citation is not None:
            state.citations = "\n".join([state.citations, web_citation])

        # Most recent web research
        existing_summary = state.running_summary
        most_recent_web_research = state.web_research_results[-1]

        updated_report = await summarize_report(
            existing_summary=existing_summary,
            new_source=most_recent_web_research,
            report_organization=report_organization,
            llm=llm,
            writer=writer
        )


        state.running_summary = updated_report

        writer({"running_summary": updated_report})

    running_summary = state.running_summary
    writer({"running_summary": running_summary})
    return {"running_summary": running_summary, "citations": state.citations}

async def finalize_summary(state: AIRAState, config: RunnableConfig, writer: StreamWriter):
    """
    Node for double checking the final summary is valid markdown
    and manually adding the sources list to the end of the report.
    """
    logger.info("FINALZING REPORT")
    llm = config["configurable"].get("llm")
    report_organization = config["configurable"].get("report_organization")

    
    writer({"final_report": "\n Starting finalization \n"})

    sources_formatted = format_sources(state.citations)
    
    # Final report creation, used to remove any remaing model commentary from the report draft
    finalizer = PromptTemplate.from_template(finalize_report) | llm
    final_buf = ""
    try:
        async with asyncio.timeout(ASYNC_TIMEOUT*3):
            async for chunk in finalizer.astream({
                "report": state.running_summary,
                "report_organization": report_organization,
            }, stream_usage=True):
                final_buf += chunk.content
                writer({"final_report": chunk.content})
    except asyncio.TimeoutError as e:
        writer({"final_report": " \n \n --------------- \n Timeout error from reasoning LLM during final report creation. Consider restarting report generation. \n \n "})
        state.running_summary = f"{state.running_summary} \n\n ---- \n\n {sources_formatted}"
        writer({"finalized_summary": state.running_summary})
        return {"final_report": state.running_summary, "citations": sources_formatted}
    
    # Strip out <think> sections
    while "<think>" in final_buf and "</think>" in final_buf:
        start = final_buf.find("<think>")
        end = final_buf.find("</think>") + len("</think>")
        final_buf = final_buf[:start] + final_buf[end:]
    
    # Handle case where opening <think> tag might be missing
    while "</think>" in final_buf:
        end = final_buf.find("</think>") + len("</think>")
        final_buf = final_buf[end:]
        
    state.running_summary = f"{final_buf} \n\n ## Sources \n\n{sources_formatted}"    
    writer({"finalized_summary": state.running_summary})
    return {"final_report": state.running_summary, "citations": sources_formatted}

def format_sources(sources: str) -> str:
    try:
        # Split sources into individual entries
        source_entries = re.split(r'(?=---\nQUERY:)', sources)
        formatted_sources = []
        src_count = 1
        
        for idx, entry in enumerate(source_entries):
            if not entry.strip():
                continue
                
            # Split into query, answer, and citations using a more precise pattern
            # This pattern looks for newlines followed by QUERY:, ANSWER:, or CITATION(S):
            # but only if they're not preceded by a pipe (|) character (markdown table)
            src_parts = re.split(r'(?<!\|)\n(?=QUERY:|ANSWER:|CITATION(?:S)?:)', entry.strip())
            
            if len(src_parts) >= 4:
                source_num = src_count
                # Remove the prefix from each part
                query = re.sub(r'^QUERY:', '', src_parts[1]).strip()
                answer = re.sub(r'^ANSWER:', '', src_parts[2]).strip()
                
                # Handle multiple citations
                citations = ''.join(src_parts[3:]) 

                formatted_entry = f"""
---
**Source** {source_num}

**Query:** {query}

**Answer:**
{answer}

{citations}
"""
                formatted_sources.append(formatted_entry)
                src_count += 1
            else:
                logger.info(f"Failed to clean up {entry} because it failed to parse")
                formatted_sources.append(entry)
                src_count += 1
                
        # Combine main content with formatted sources
        return "\n".join(formatted_sources)
    except Exception as e:
        logger.warning(f"Error formatting sources: {e}")
        return sources
    
# The following nodes are biomed aira nodes

async def check_virtual_screening_intended(llm, writer, report_organization: str, topic : str) -> bool:
    """
    Check the report_organization to determine if virtual screening is intended to happen.
    Returns True or False.
    """
    
    response = await llm.ainvoke(check_whether_virtual_screening.format(report_organization=report_organization, topic = topic))
    intention = parse_json_markdown(response.content)
    writer({"check_virtual_screening_intended": "Intention of virtual screening: " + intention["intention"].lower()})
    if intention["intention"].lower() == "yes":
        return True
    else:
        return False
    

async def find_protein_and_molecule( llm, topic, writer, config, collection, search_web, num_iterations = 3):
    """
    This function creates and sends queries sent to the RAG/web to find the two items needed to kick off virtual screening: 
    target protein, and recent small molecule therapy.
    """
    logger.info("FIND TARGET PROTEIN and SMALL MOLECULE THERAPY FROM KNOWLEDGE BASE")
    vs_additional_queries = []
    vs_queries_results = []
    vs_citations = ""
    target_prot, sml_molecule = "", ""

    for i in range(num_iterations):
        writer({"find_protein_and_molecule": f"\n Iteration: {str(i)} \n"})
        if len(vs_queries_results) == 0:
            knowledge_sources = "Not existing knowledge found. "
        else:
            knowledge_sources = "\n".join(vs_queries_results)
        input = {
            "input": check_protein_molecule_found.format(topic = topic, knowledge_sources=knowledge_sources)

        }
        system_prompt = ""
        if hasattr(llm, "model_name") and llm.model_name == "nvidia/llama-3.3-nemotron-super-49b-v1":
            system_prompt = "detailed thinking on"

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system", system_prompt
                ),
                (
                    "human", "{input}"
                ),
            ]
        )
        chain = prompt | llm

        writer({"find_protein_and_molecule": "\n Starting the check among existing virtual screening query results. \n "})
        async for i in async_gen(1):
            result = ""
            stop = False
            async for chunk in chain.astream(input, stream_usage=True):
                result = result + chunk.content
                if chunk.content == "</think>":
                    stop = True
                if not stop:
                    writer({"find_protein_and_molecule": chunk.content})

        splitted = result.split("</think>")
        if len(splitted) < 2:
            # If we can't parse anything
            continue
        # get the remaining queries needed to have both of the ingredients for virtual screening
        response_json = splitted[1].strip()
        writer({"find_protein_and_molecule": f"\n Returned result: {response_json} \n "})
        try:
            response_obj = parse_json_markdown(response_json)
            if "query" in response_obj:
                gen_query = GeneratedQuery(
                    query=response_obj["query"] if "query" in response_obj else str(response_obj),
                    report_section="Virtual Screening Details",
                    rationale="Remaining query needed for gathering the two ingredients needed for virtual screening"
                )
                vs_additional_queries.append(gen_query)
                rag_answer, rag_citation, relevancy, web_answer, web_citation = await process_single_query(
                    query=gen_query.query,
                    config=config,
                    writer=writer,
                    collection=collection,
                    llm=llm,
                    search_web=search_web
                )
                vs_search_str = deduplicate_and_format_sources(
                    [rag_citation], [rag_answer], [relevancy], [web_answer], [gen_query]
                )

                vs_queries_results.append(vs_search_str)
                writer({"find_protein_and_molecule": f"\n This query's search results: {vs_search_str} \n "})
                if relevancy['score'] == "yes" and rag_citation is not None:
                    vs_citations = "\n".join([vs_citations, rag_citation])

                if relevancy['score'] != "yes" and web_citation not in ["N/A", ""] and web_citation is not None:
                    vs_citations = "\n".join([vs_citations, web_citation])
                
            elif "target_protein" in response_obj and "recent_small_molecule_therapy" in response_obj:
                target_prot, sml_molecule = response_obj["target_protein"],  response_obj["recent_small_molecule_therapy"]
                break
        except Exception as e:
            logger.warning(f"Error parsing reflection JSON: {e}")
            
       
    writer({"find_protein_and_molecule": f"\nCitations: {vs_citations} \n "})
    
    writer({"find_protein_and_molecule": f"\nTarget protein is {target_prot} \n "})
    writer({"find_protein_and_molecule": f"\nSmall molecule therapy is {sml_molecule} \n "})
    writer({"find_protein_and_molecule": "\nNow leaving the checking function."})
    return target_prot, sml_molecule, vs_additional_queries, vs_queries_results, vs_citations


async def begin_virtual_screening_if_intended(
        state: AIRAState,
        config: RunnableConfig,
        writer: StreamWriter
):
    """
    After web_research is performed, each query had been sent to RAG and optionally a web search, 
    with results and citations for each query.
    Check if virtual screening is intended, and check if the two items needed for virtual screening are 
    present in the web_research results.
    """
    llm = config["configurable"].get("llm")
    report_organization = config["configurable"].get("report_organization")
    num_reflections = config["configurable"].get("num_reflections")
    topic = config["configurable"].get("topic")
    collection = config["configurable"].get("collection")
    search_web = config["configurable"].get("search_web")

    vs_intended = await check_virtual_screening_intended(llm, writer, report_organization, topic)
    if not vs_intended:
        logger.info("VIRTUAL SCREENING IS NOT INTENDED")
        # Virtual Screening is not intended, no need to start virtual screening
        
    else:
        logger.info("VIRTUAL SCREENING IS INTENDED")
        # Virtual Screening is intended, next, check whether the last web_research contained 
        # the necessary info for starting VS: target protein and recent small molecule therapy
        most_recent_web_research = state.web_research_results[-1] 
        state.target_protein, state.recent_sml_molecule, state.vs_queries, state.vs_queries_results, state.vs_citations = await find_protein_and_molecule(llm, topic, writer, config, collection, search_web)
        logger.info("TARGET PROTEIN AND RECENT SML MOLECULE HAVE BEEN FOUND")
        
    state.do_virtual_screening = vs_intended
    return {"do_virtual_screening": state.do_virtual_screening, "target_protein": state.target_protein, "recent_sml_molecule": state.recent_sml_molecule, "vs_queries_results": state.vs_queries_results, "vs_citations": state.vs_citations, "vs_additional_queries": state.vs_queries}

def pdb_to_string(pdb_filepath: str):
    """
    Reads a PDB file and returns its content as a string.

    Args:
        pdb_filepath (str): The path to the PDB file.

    Returns:
        str: The content of the PDB file as a string, or None if an error occurs.
    """
    try:
        with open(pdb_filepath, 'r') as file:
            pdb_string = file.read()
        return pdb_string
    except FileNotFoundError:
        logger.info(f"Error: File not found: {pdb_filepath}")
        return None
    except Exception as e:
         logger.info(f"An error occurred: {e}")
         return None

async def generate_molecule(molecule: str, molmim_invoke_url: str ) -> str:
    """Run a molecular generation model to generate molecules similar to a target molecule. 
    This returns generated ligands in SMILES format.
    If using self hosted url, make sure the url includes /generate at the end
    """
    logger.info("STARTING TO CALL MOLMIM NIM")
    # Code for hosted molmim model
    NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
    logger.info("USING NVIDIA_API_KEY (not needed if self hosting MolMIM): " + NVIDIA_API_KEY)
    logger.info("Using the MolMIM URL: " + molmim_invoke_url)
    
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Accept": "application/json",
    }
    payload = {
        'smi': molecule,
        'num_molecules': 3,
        'algorithm': 'CMA-ES',
        'property_name': 'QED',
        'min_similarity': 0.7, # Ignored if algorithm is not "CMA-ES".
        'iterations': 10,
    }
    session = requests.Session()

    if molmim_invoke_url == "https://health.api.nvidia.com/v1/biology/nvidia/molmim/generate":
        # if using public endpoint, need to pass in NVIDIA_API_KEY
        response = session.post(molmim_invoke_url, headers=headers, json=payload)
        response.raise_for_status()
        response_body = response.json()
        molecules = json.loads(response_body['molecules'])
        generated_ligands = '\n'.join([v['sample'] for v in molecules])
    else:
        # self hosting NIM, no need for NVIDIA_API_KEY. This has been tested with version nvcr.io/nim/nvidia/molmim:1.0.0
        response = session.post(molmim_invoke_url, json=payload)
        response.raise_for_status()
        response_body = response.json()
        generated_ligands = '\n'.join(v["smiles"] for v in response_body['generated'])
    return(generated_ligands)

async def dock_molecule(curr_out_dir: str, folded_protein: str, generated_ligands: str, diffdock_invoke_url: str ):
        """Run a molecular docking to generate the docking poses and scores for generated_ligands. Return true if docking is successful, false otherwise."""
        logger.info("STARTING TO CALL DIFFDOCK NIM")
        NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
        logger.info("USING NVIDIA_API_KEY (not needed if self hosting DiffDock): " + NVIDIA_API_KEY)
        logger.info("Using the DiffDock URL: " + diffdock_invoke_url)
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Accept": "application/json",
        }
        payload={
            'protein': folded_protein,
            'ligand': generated_ligands,
            'ligand_file_type': 'txt',
            'num_poses': 10,
            'time_divisions': 20,
            'num_steps': 18,
            'save_trajectory': 'true',
        }
        docking_status = ""
        
        try:
            if diffdock_invoke_url == "https://health.api.nvidia.com/v1/biology/mit/diffdock":
                # if using public endpoint, need the pass in the NVIDIA_API_KEY
                response = requests.post(diffdock_invoke_url, headers=headers, json=payload)
            else:
                # self hosted URL, no need for NVIDIA_API_KEY. This has been tested with version nvcr.io/nim/mit/diffdock:2.1.0
                response = requests.post(diffdock_invoke_url, headers={"Accept": "application/json"}, json=payload)
            response.raise_for_status()
            response_body = response.json()
            
            diffdock_position_confidence = response_body["position_confidence"] 
            ret_conf_scores = []
            for i in range(10):
                current_pos = []
                for j in range(3):
                    current_pos.append(diffdock_position_confidence[j][i])
                ret_conf_scores.append(current_pos)
            
            logger.info("Confidence scores from diffdock:\n"+ " \n".join(str(sc) for sc in ret_conf_scores) )
            with open(os.path.join(curr_out_dir, 'confidence_scores.csv'), 'w', newline='') as f:
                            writer = csv.writer(f)
                            writer.writerows(ret_conf_scores)
            
            
            for i in range(len(response_body['ligand_positions'])):
                
                if isinstance(response_body['ligand_positions'][i], list):
                        for j in range(len(response_body['ligand_positions'][i])):
                                with open(os.path.join(curr_out_dir, f'ligand_{i}_{j}.mol'), "w") as f:
                                    f.write(response_body['ligand_positions'][i][j])
                else:        
                    with open(os.path.join(curr_out_dir, f'ligand_{i}.mol'), "w") as f:
                        f.write(response_body['ligand_positions'][i])
            docking_status += f"\n The docking in DiffDock has been completed. The 3 proposed molecules returned the docking success status of: [{", ".join(response_body["status"])}]. \n"
            docking_status += f"The position confidence scores have been stored in file: {os.path.join(curr_out_dir, 'confidence_scores.csv')}, and they are: \n {" \n ".join(str(sc) for sc in diffdock_position_confidence)}. \n "
            docking_status += f"\n The docking ligand positions have been saved into {len(response_body['ligand_positions']) * len(response_body['ligand_positions'][i])} .mol files in directory: {curr_out_dir}. \n "
            return docking_status
        
        except Exception as e:
            logger.info(f"An error occurred: {e}")
            ret_conf_scores_zero = [[0 for j in range(3)] for i in range(10)]
            with open(os.path.join(curr_out_dir, 'confidence_scores.csv'), 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerows(ret_conf_scores_zero)
            docking_status += "\n The docking in DiffDock failed. " + f"An error occurred: {e}. \n "
            return docking_status


def get_smiles_from_molecule_name(compound_name: str, writer: StreamWriter):
    compounds = pcp.get_compounds(compound_name, 'name')
    writer_info = ""
    if len(compounds) == 0:
        # no compound has been found with the name, try with a different name
        writer_info_new = f"\nPreparation step - Could not find a molecule from name: {compound_name}. \n "
        writer_info += writer_info_new
        writer({"call_virtual_screening_nims": writer_info_new})
        return None, writer_info
    else:
        for compound in compounds:
            writer_info_new = f"\nPreparation step - Found SMILES string: {str(compound.isomeric_smiles)} for molecule {str(compound.cid)} with name {compound_name} in pubchem. \n "
            writer_info += writer_info_new
            writer({"call_virtual_screening_nims": writer_info_new})
            
        if len(compounds) > 1:
            writer_info_new = "\nPreparation step - Multiple molecule found in pubchem, using the first molecule's SMILES string. \n "
            writer_info += writer_info_new
            writer({"call_virtual_screening_nims": writer_info_new})
        # return the first found molecule's SMILES string if there are more than one molecule found
        return str(compounds[0].isomeric_smiles), writer_info

def get_protein_id_from_name(protein_name: str, writer: StreamWriter):
    q1 = TextQuery(protein_name)
    q2 = AttributeQuery(
        attribute="rcsb_entity_source_organism.scientific_name",
        operator="exact_match",
        value="Homo sapiens"
    )
    q3 = AttributeQuery(
        attribute="exptl.method", 
        operator="exact_match", 
        value="electron microscopy"
    )
    query = q1 & (q2  & q3)
    writer_info = ""
    writer_info_new = f"\nPreparation step - looking for a protein ID from protein name {protein_name}, source organism must be homo sapiens and experimental method must be electron microscopy. \n "
    writer_info += writer_info_new
    writer({"call_virtual_screening_nims": writer_info_new})

    # Execute the query by running it as a function
    results = query()

    # Results are returned as an iterator of result identifiers.
    first_id = None
    other_ids = []
    for rid in results:
        rid = str(rid)
        if first_id == None:
            # just return the first protein ID found from the search, this can be refined
            first_id = rid
            writer_info_new = f"\nPreparation step - the first protein ID {first_id} found from protein name: {protein_name} \n "
            writer_info += writer_info_new
            writer({"call_virtual_screening_nims": writer_info_new})
        else:
            other_ids.append(rid)
    if first_id == None:
        writer_info_new = f"\nPreparation step - could not find protein ID from protein name: {protein_name} \n "
        writer_info += writer_info_new
        writer({"call_virtual_screening_nims": writer_info_new})
    if len(other_ids) > 0:
        writer_info_new = f"\nPreparation step - there were other protein IDs found from protein name {protein_name} : {" ".join(other_ids)} \n "
        writer_info += writer_info_new
        writer({"call_virtual_screening_nims": writer_info_new})
    return first_id, writer_info

def download_pdb_from_protein_id(protein_id: str, output_dir: str, writer: StreamWriter):
    url = f"https://files.rcsb.org/download/{protein_id}.pdb"
    response = requests.get(url)
    writer_info = ""
    if response.status_code == 200:
        filename = os.path.join(output_dir, f"{protein_id}.pdb")
        with open(filename, "wb") as file:
            file.write(response.content)
        print("File downloaded successfully!")
        writer_info_new = f"\nPreparation step - downloaded pdb file from url {url} to location {filename} \n "
        writer_info += writer_info_new
        writer({"call_virtual_screening_nims": writer_info_new})
        return filename, writer_info
    else:
        logger.info(f"When trying to download {url} for the protein PDF file, an error occurred. Status code: {response.status_code}")
        writer_info_new =  f"\nPreparation step - failed to download pdb file from url: {url} \n "
        writer_info += writer_info_new
        writer({"call_virtual_screening_nims": writer_info_new})
        return None, writer_info

async def call_virtual_screening_nims(
        state: AIRAState,
        config: RunnableConfig,
        writer: StreamWriter
):
    """
    Call the virtual screening nims with the inputs of target protein and recent small molecule therapy.
    """
   
    # don't do anything if there is no intention to do virtual screening
    if not state.do_virtual_screening:
        logger.info("ABANDONING VIRTUAL SCREENING: State's do_virtual_screening is FALSE")
        return
    # proceed if there is intention to do virtual screening
    # first get the target protein's pdb format
    # secondly get the small molecule therapy's SMILES string
    writer_info = ""
    logger.info("THE TARGET PROTEIN IS: " + state.target_protein)
    logger.info("THE RECENT SMALL MOLECULE THERAPY IS: " + state.recent_sml_molecule)
    writer_info_new = f"\nUsing the following target protein and recent small molecule therapy for calls to virtual screening NIM: {state.target_protein}, {state.recent_sml_molecule}. \n "
    writer_info += writer_info_new
    writer({"call_virtual_screening_nims": writer_info_new})
    logger.info("STARTING TO CALL VIRTUAL SCREENING NIMS")

    protein_id, add_writer_info = get_protein_id_from_name(state.target_protein, writer)
    writer_info += add_writer_info
    molecule, add_writer_info = get_smiles_from_molecule_name(state.recent_sml_molecule, writer)
    writer_info += add_writer_info

    #pdb_filepath = 'app/src/aiq_aira/8EIQ-Prepared-truncated-KG_19FEB2025.pdb'
    #molecule = "CC(C)(C)C1=CC(=C(C=C1NC(=O)C2=CNC3=CC=CC=C3C2=O)O)C(C)(C)C" # ivacaftor
    if protein_id == None:
        # didn't find a protein from the protein name
        writer_info_new = f"\nAbandoning virtual screening due to a lack of proteins found from protein name: {state.target_protein} \n "
        writer_info += writer_info_new
        writer({"call_virtual_screening_nims": writer_info_new})
        state.vs_steps_info = writer_info
        return {"vs_steps_info": writer_info}
        
    if molecule == None:
        writer_info_new = f"\nAbandoning virtual screening due to a lack of molecules found from molecule name: {state.recent_sml_molecule} \n "
        writer_info += writer_info_new
        writer({"call_virtual_screening_nims": writer_info_new})
        state.vs_steps_info = writer_info
        return {"vs_steps_info": writer_info}
    
    try:
        curr_out_dir = os.path.join("virtual_screening_output" , datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S"))
        os.makedirs(curr_out_dir, exist_ok=True)
        assert os.path.isdir(curr_out_dir)
    except Exception as e:
        logger.info(f"An error occurred in creating the output directory {curr_out_dir}: {e}")

    try:
        pdb_filepath, add_writer_info = download_pdb_from_protein_id(protein_id, curr_out_dir, writer)
        writer_info += add_writer_info
        if pdb_filepath == None:
            logger.info(f"Could not download the PDB file with protein id {protein_id} in download_pdb_from_protein_id()")
    except Exception as e:
        logger.info(f"An error occurred in download_pdb_from_protein_id: {e}")
    try:
        protein_structure = pdb_to_string(pdb_filepath)
    except Exception as e:
        logger.info(f"An error occurred in pdb_to_string: {e}")
    try:
        molmim_endpoint_url = os.getenv("MOLMIM_ENDPOINT_URL")
        generated_ligands =  await generate_molecule(molecule=molecule, molmim_invoke_url=molmim_endpoint_url)
       
        writer_info_new =  "\nThe generated ligands from MolMIM are: \n " + generated_ligands.replace("\n", " \n ") + " \n "
        writer_info += writer_info_new
        writer({"call_virtual_screening_nims": writer_info_new})
    except Exception as e:
        logger.info(f"An error occurred in generate_molecule: {e}")
    try:
        diffdock_endpoint_url = os.getenv("DIFFDOCK_ENDPOINT_URL")
        add_writer_info =  await dock_molecule(curr_out_dir, protein_structure, generated_ligands, diffdock_endpoint_url)
        writer_info += add_writer_info
        writer({"call_virtual_screening_nims": add_writer_info})
    except Exception as e:
        logger.info(f"An error occurred in dock_molecule: {e}")
    state.vs_steps_info = writer_info
    return {"vs_steps_info": writer_info}




async def combine_virtual_screening_info_into_summary(state: AIRAState, config: RunnableConfig, writer: StreamWriter):
    """
    Reflect on the summary to find knowledge gaps, producing an updated query if needed.
    """
    if not state.do_virtual_screening:
        logger.info("No need to combine virtual screening info into summary since virtual screening was not performed.")
        return
    logger.info("COMBINING VIRTUAL SCREENING PROCESS AND RESULTS INTO THE SUMMARY")
    llm = config["configurable"].get("llm")
    report_organization = config["configurable"].get("report_organization")


    
    input = {
        "input": combine_virtual_screening_info_into_report_prompt.format(report_organization=report_organization, 
                                                                          report=state.running_summary, 
                                                                          vs_info=state.vs_steps_info,
                                                                          vs_queries = state.vs_queries,
                                                                          vs_queries_results = state.vs_queries_results)

    }
    system_prompt = ""
    if hasattr(llm, "model_name") and llm.model_name == "nvidia/llama-3.3-nemotron-super-49b-v1":
        system_prompt = "detailed thinking on"

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system", system_prompt
            ),
            (
                "human", "Add virtual screening steps and info into the existing report draft. {input}"
            ),
        ]
    )
    chain = prompt | llm

    
    result = ""
    stop = False


    try: 
        writer({"add_virtual_screening_info_into_report": "\n Starting to combine virtual screening info into exising report draft \n"})
        async with asyncio.timeout(ASYNC_TIMEOUT):
            async for chunk in chain.astream(input, stream_usage=True):
                result += chunk.content
                if chunk.content == "</think>":
                    stop = True
                if not stop:
                    writer({"add_virtual_screening_info_into_report": chunk.content})
    except asyncio.TimeoutError as e:
        writer({"add_virtual_screening_info_into_report": " \n \n ---------------- \n \n Timeout error from reasoning LLM. Consider running report combination again. \n \n "})
        # update nothing and just return
        return 

    # Remove <think>...</think> sections
    while "<think>" in result and "</think>" in result:
        start = result.find("<think>")
        end = result.find("</think>") + len("</think>")
        result = result[:start] + result[end:]
        state.running_summary = result
    
    # Handle case where opening <think> tag might be missing
    while "</think>" in result:
        end = result.find("</think>") + len("</think>")
        result = result[end:]
        state.running_summary = result

    # Return the final updated summary
    writer({"running_summary_with_virtual_screening_info": state.running_summary})
    if state.vs_citations != None :
        state.citations = "\n".join([state.citations, state.vs_citations])
    else:
        writer({"running_summary_with_virtual_screening_info": " \n VIRTUAL SCREENING QUERIES CITATIONS ARE EMPTY \n "})
    return {"running_summary": state.running_summary, "citations": state.citations}
