"""
The script takes a .tsv file generated by the combine_str_json_to_tsv.py script for ExpansionHunter calls at
genome-wide loci.
It then prints out the subset of rows that might represent pathogenic expansions and should be reviewed in more detail.

The input .tsv must have at least these input columns:

    "LocusId", "SampleId", "Sample_affected", "Sample_sex",
    "Num Repeats: Allele 1", "Num Repeats: Allele 2", "CI end: Allele 1", "CI end: Allele 2",

One way to add the  Sample_* and VariantCatalog_* columns is to run combine_str_json_to_tsv.py with the
--sample-metadata and  the --variant-catalog args. Variant catalogs can be taken from the variant_catalogs directory in
this github repo.
"""

import argparse
import collections
import json
import os
import subprocess
from ast import literal_eval

import pandas as pd
from tabulate import tabulate

from str_analysis.utils.canonical_repeat_unit import compute_canonical_motif


REQUIRED_COLUMNS = [
    "LocusId",
    "Num Repeats: Allele 1",
    "Num Repeats: Allele 2",
]

SEPARATOR_STRING = "---"

def run(command):
    print(command)
    subprocess.check_call(command, shell=True)



def parse_args():
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--sample-metadata-table", help="Optional path of a table that contains sample metadata columns. "
        "If specified, this table will be joined with the combined_tsv_table before proceeding with the analysis.")
    p.add_argument("--metadata-table-sample-id-column", default="sample_id", help="Name of the column in the metadata "
        "table that contains the sample id")

    p.add_argument("--known-pathogenic-variant-catalog", help="Optional path of an ExpansionHunter variant catalog in "
        " JSON format. If specified, it will be used to retrieve pathogenic thresholds for known disease loci.")
    grp = p.add_argument_group()
    grp.add_argument("--use-affected", action="store_true", help="Use affected status to determine which samples to "
        "include in the output for each locus. Only include those affected samples that have larger expansions than "
        "the top N unaffected samples (adjustsable by --max-n-unaffected).")
    grp.add_argument("--use-thresholds", action="store_true", help="Use known pathogenic thresholds to determine which "
        "samples to include in the output for each locus. All samples with expansions above the pathogenic threshold "
        "will be included.")

    p.add_argument("--locus-id", help="Filter to a single locus id for debugging")
    p.add_argument("--motif", help="If specified, only loci with this motif (normalized) will be processed")
    p.add_argument("--min-motif-size", type=int, help="If specified, only loci with motifs of this size or larger "
        "will be considered")

    p.add_argument("--threshold", type=int, help="If specified, only loci with expansions above this number of repeats "
        "will be considered")
    p.add_argument("--number-to-threshold", choices=("Num Repeats", "CI end"), default="Num Repeats", help="Which column "
        "to use for the threshold comparison")
    p.add_argument("--inheritance-mode", default="AD", choices=("AD", "AR", "XR"), help="Optional assumed inheritance mode")
    p.add_argument("--purity-threshold", type=float, help="TRGT purity threshold")

    p.add_argument("-n", "--max-rows", type=int, default=10000000, help="Limit the max number of samples to include in "
        "the output for each locus.")
    p.add_argument("--max-n-unaffected", type=int, default=10, help="After this many unaffected samples are "
        "encountered with genotypes above a particular expansion size at locus, all samples (affected or unaffected) "
        "that have smaller expansions at that locus will be ignored")

    p.add_argument("-l", "--locus", action="append", help="If specified, only these locus ids will be processed")
    p.add_argument("combined_tsv_path", help="Path of combined ExpansionHunter .tsv table generated by the "
        "combine_str_json_to_tsv.py script. It's assumed that combine_str_json_to_tsv.py "
        "was run with --sample-metadata and --variant-catalog args to add sample-level and locus-level metadata columns")

    p.add_argument("--motif-column", default="RepeatUnit", help="Name of the column in the combined table that "
                   "contains the locus motif")
    p.add_argument("--sample-id-column", default="SampleId", help="Name of the column in the combined table that "
                   "contains the sample id")
    p.add_argument("--sample-affected-status-column", default="Sample_affected", help="Name of the column in the "
        "combined table that contains the sample affected status")
    p.add_argument("--sample-sex-column", default="Sample_sex", help="Name of the column in the combined table that "
        "contains the sample sex")
    p.add_argument("--sample-analysis-status-column", default="Sample_analysis_status", help="Name of the column in "
                   "the combined table that contains the sample analysis status")
    p.add_argument("--sample-phenotypes-column", default="Sample_phenotypes", help="Name of the column in the combined "
                   "table that contains the list of HPO terms for this sample")
    p.add_argument("--sample-paternal-id-column", default="paternal_id", help="Name of the column in the combined "
                   "table that contains the paternal sample id")
    p.add_argument("--sample-maternal-id-column", default="maternal_id", help="Name of the column in the combined "
                   "table that contains the maternal sample id")
    p.add_argument("--output-prefix", help="Prefix of TSV table with results that pass thresholds.",
                   default="passed")

    # Parse and validate command-line args + read in the combined table(s) from the given command_tsv_path(s)
    args = p.parse_args()
    if not os.path.isfile(args.combined_tsv_path):
        p.error(f"{args.combined_tsv_path} not found")

    if args.sample_metadata_table and not os.path.isfile(args.sample_metadata_table):
        p.error(f"{args.sample_metadata_table} not found")

    if args.known_pathogenic_variant_catalog and not os.path.isfile(args.known_pathogenic_variant_catalog):
        p.error(f"{args.known_pathogenic_variant_catalog} not found")

    if not args.use_affected and not args.use_thresholds and not args.threshold:
        p.error("Must specify --use-thresholds or --use-affected or --threshold")

    return args


def compute_threshold_lookup_by_motif(variant_catalog_path):
    with open(variant_catalog_path) as f:
        variant_catalog = json.load(f)

    threshold_lookup = {}
    for record in variant_catalog:
        if "LocusId" not in record:
            print(f"WARNING: 'LocusId' key not found in variant catalog record for {pformat(record)}")
            continue

        if set(record["RepeatUnit"]) - set("ACGT"):
            print(f"Skipping {locus_id} because motif contains non-ACGT bases: {record['RepeatUnit']}")
            continue

        locus_id = record["LocusId"]
        try:
            pathogenic_min_thresholds = [
                int(disease_record["PathogenicMin"]) for disease_record in record.get("Diseases", []) if "PathogenicMin" in disease_record
            ]

            if not pathogenic_min_thresholds:
                print(f"WARNING: Did not find any 'Diseases' records with a 'PathogenicMin' key for {locus_id}")
                continue

            inheritance_modes = {
                disease_record.get("InheritanceMode", "AD") for disease_record in record.get("Diseases", []) if "Inheritance" in disease_record
            }

            inheritance_mode = next(iter(inheritance_modes))
            if len(inheritance_modes) > 1:
                print(f"WARNING: Multiple inheritance modes found for {locus_id}: {inheritance_modes}. Using the first one: ", inheritance_mode)


            min_threshold = min(pathogenic_min_thresholds)
            canonical_motif = compute_canonical_motif(record["RepeatUnit"])
            if canonical_motif == "CCG" and min_threshold < 10:
                print(f"WARNING: Skipping {locus_id} because canonical motif is CCG and min threshold is {min_threshold}")
                continue

            if canonical_motif not in threshold_lookup:
                print(f"Adding {canonical_motif} with threshold {min_threshold} for {locus_id}")
                threshold_lookup[canonical_motif] = (min_threshold, inheritance_mode)
            else:
                previous_threshold = threshold_lookup[canonical_motif][0]
                min_threshold = min(min_threshold, previous_threshold)
                if min_threshold != previous_threshold:
                    print(f"Updating {canonical_motif} threshold to {min_threshold} for {locus_id}")
                threshold_lookup[canonical_motif] = (min_threshold, inheritance_mode)


        except Exception as e:
            print(f"WARNING: Unable to parse pathogenic threshold for {locus_id}: {e}")
            continue

    return threshold_lookup


def filter_by_purity(min_purity_threshold, both_alleles=False):
	def filter_func(allele_purity_values):
		if both_alleles:
			return all(p != "." and float(p) >= min_purity_threshold for p in allele_purity_values.split(","))
		else:
			allele_purity_values = allele_purity_values.split(",")
			p = allele_purity_values[-1]
			return p != "." and float(p) >= min_purity_threshold

	return filter_func


def normalize_column_name(c):
    """Normalize column name"""
    return c.replace(" ", "_").replace(":", "_").replace(",", "_")

def load_results_table(args):

    sample_metadata_df = None
    if args.sample_metadata_table:
        print(f"Reading {args.sample_metadata_table}")
        sample_metadata_df = pd.read_table(args.sample_metadata_table, dtype=str)
        if args.metadata_table_sample_id_column not in sample_metadata_df.columns:
            raise ValueError(f"Metadata table {args.sample_metadata_table} is missing the sample id column: "
                             f"{args.metadata_table_sample_id_column}. Use --metadata-table-sample-id-column to "
                             f"specify the correct sample id column name in this table.")
        sample_metadata_df.set_index(args.metadata_table_sample_id_column, inplace=True)
        print(f"Read {len(sample_metadata_df):,d} rows from {args.sample_metadata_table}")
        print(f" - using sample id column: {args.metadata_table_sample_id_column}")

    # read in table
    print(f"Reading {args.combined_tsv_path}")
    df = pd.read_table(args.combined_tsv_path, low_memory=False, dtype=str)
    print(f"Read {len(df):,d} rows ({len(set(df.LocusId)):,d} loci) from {args.combined_tsv_path}")
    print(f" - using sample id column: {args.sample_id_column}")

    if args.locus_id:
        print(f"Filtering to locus id {args.locus_id}")
        df = df[df.LocusId == args.locus_id]
        if len(df) == 0:
            raise ValueError(f"{args.locus_id} not found in {args.combined_tsv_path}")

    if args.sample_id_column not in df.columns:
        raise ValueError(f"Missing sample id column: {args.sample_id_column} in {args.combined_tsv_path}: {list(df.columns)}")

    duplicated_df = df[df.duplicated(subset=["VariantId", args.sample_id_column])]
    if len(duplicated_df) > 0:
        print("Duplicate rows will be discarded:")
        print(tabulate(duplicated_df.iloc[0:50], headers="keys", tablefmt="psql")) #, showindex=range(1, len(duplicated_df)+1)))
        before = len(df)
        df.drop_duplicates(subset=["VariantId", args.sample_id_column], inplace=True)
        if len(df) < before:
            print(f"WARNING: Removed {before - len(df):,d} duplicate rows out of {before:,d} total rows ({(before - len(df))/before:0.1%})")

    df["CanonicalMotif"]  = df["RepeatUnit"].apply(compute_canonical_motif)
    if args.motif:
        before = len(df)
        df = df[df["CanonicalMotif"] == compute_canonical_motif(args.motif)]
        print(f"Kept {len(df):,d} out of {before:,d} rows ({len(set(df.LocusId)):,d} loci) with canonical motif {args.motif}")

    if args.min_motif_size:
        before = len(df)
        df = df[df["RepeatUnit"].str.len() >= args.min_motif_size]
        print(f"Kept {len(df):,d} out of {before:,d} rows ({len(set(df.LocusId)):,d} loci) with motif size >= {args.min_motif_size}")

    if sample_metadata_df is not None:
        len_before_join = len(df)
        df = df.set_index(args.sample_id_column).join(sample_metadata_df, how="left").reset_index().rename(columns={
            "index": args.sample_id_column,
        })
        if len_before_join != len(df):
            raise ValueError(f"Sample metadata table join error: expected {len_before_join:,d} rows, got {len(df):,d}")

    if args.purity_threshold:
        before = len(df)
        check_both_alleles = args.inheritance_mode == "AR" or args.inheritance_mode == "XR"
        df = df[df["AllelePurity"].apply(filter_by_purity(args.purity_threshold, both_alleles=check_both_alleles))]
        print(f"Kept {len(df):,d} out of {before:,d} ({len(df)/before:0.1%}) rows ({len(set(df.LocusId)):,d} loci) that passed purity threshold of {args.purity_threshold}")


    print(f"Calculating columns")
    df.loc[:, "Num Repeats: Allele 1"] = df["Num Repeats: Allele 1"].fillna(0).astype(float).astype(int)
    df.loc[:, "Num Repeats: Allele 2"] = df["Num Repeats: Allele 2"].fillna(0).astype(float).astype(int)

    # replace NA with "Unknown" strings
    #df.loc[:, args.sample_affected_status_column] = df[args.sample_affected_status_column].fillna("Unknown")
    df.loc[:, args.sample_affected_status_column] = df[args.sample_affected_status_column].replace({
        "Unaffected": "Not Affected",
        "Possibly Affected": "Unknown",
        "Possibly affected": "Unknown",
    })

    # add Paternal and Maternal genotypes to each row where applicable
    genotype_map = {}
    if (args.sample_paternal_id_column and args.sample_paternal_id_column in df.columns) or (
        args.sample_maternal_id_column and args.sample_maternal_id_column in df.columns):

        sample_and_locus_id_to_genotype_map = {}
        for row in df.itertuples():
            sample_id = getattr(row, args.sample_id_column)
            locus_id = row.LocusId
            if not pd.isna(row.Genotype):
                sample_and_locus_id_to_genotype_map[(sample_id, locus_id)] = row.Genotype

        if args.sample_paternal_id_column and args.sample_paternal_id_column in df.columns:
            df["PaternalGenotype"] = df.apply(lambda r: sample_and_locus_id_to_genotype_map.get(
                (r[args.sample_paternal_id_column], r.LocusId)), axis=1)
            if all(df["PaternalGenotype"].isna()):
                df.drop(columns=["PaternalGenotype"], inplace=True)

        if args.sample_maternal_id_column and args.sample_maternal_id_column in df.columns:
            df["MaternalGenotype"] = df.apply(lambda r: sample_and_locus_id_to_genotype_map.get(
                (r[args.sample_maternal_id_column], r.LocusId)), axis=1)
            if all(df["MaternalGenotype"].isna()):
                df.drop(columns=["MaternalGenotype"], inplace=True)

    df_unique_sample_ids = df.drop_duplicates([args.sample_id_column])
    print("Affected status counts:", dict(df_unique_sample_ids[args.sample_affected_status_column].value_counts()))
    print(f"Examples of {args.sample_id_column} with Unknown affected status:", ", ".join(
        df[df[args.sample_affected_status_column] == "Unknown"][args.sample_id_column][0:10]))
    if sum(~df[args.sample_affected_status_column].isin({"Affected", "Not Affected", "Unknown"})) > 0:
        print(f"Examples of {args.sample_id_column} with other affected status:", ", ".join(
            df[df[args.sample_affected_status_column].isin({"Affected", "Not Affected", "Unknown"})][args.sample_id_column][0:10]))
    unexpected_affected_column_values = set(df[args.sample_affected_status_column]) - {"Affected", "Not Affected", "Unknown"}
    if unexpected_affected_column_values:
        raise ValueError(f"Unexpected affected status values: {unexpected_affected_column_values}:  {collections.Counter(df[args.sample_affected_status_column])}")

    print("Sample sex counts:", dict(df_unique_sample_ids[args.sample_sex_column].value_counts()))
    unexpected_sample_sex_column_values =  set(df[args.sample_sex_column].str.lower()) - {"m", "male", "f", "female"}
    if unexpected_sample_sex_column_values:
        raise ValueError(f"Unexpected {args.sample_sex_column} values: {unexpected_sample_sex_column_values}:  {collections.Counter(df[args.sample_sex_column])}")

    # check that all required columns are present
    missing_required_columns = (
        set(REQUIRED_COLUMNS) | {args.sample_sex_column, args.sample_affected_status_column}
    ) - set(df.columns)

    if missing_required_columns:
        raise ValueError(f"{args.combined_tsv_path} is missing these required columns: {missing_required_columns}")

    df.loc[:, "is_male"] = df[args.sample_sex_column].str.lower().str.startswith("m")  # this leaves missing values as Na
    df.loc[:, args.sample_sex_column] = df[args.sample_sex_column].fillna("Unknown")

    if args.inheritance_mode == "XR":
        # keep only chrX loci and male samples
        df = df[df["is_male"]]
        df = df[df["ReferenceRegion"].str.startswith("X") | df["ReferenceRegion"].str.startswith("chrX")]

    print(f"Calculating additional columns")
    df.loc[:, "Num Repeats: Min Allele 1, 2"] = df.apply(lambda row: (
        min(row["Num Repeats: Allele 1"], row["Num Repeats: Allele 2"])
        if
        row["Num Repeats: Allele 1"] > 0 and row["Num Repeats: Allele 2"] > 0
        else
        max(row["Num Repeats: Allele 1"], row["Num Repeats: Allele 2"])
    ), axis=1)

    print(f"Calculating additional columns 2")
    df.loc[:, "Num Repeats: Max Allele 1, 2"] = df[["Num Repeats: Allele 1", "Num Repeats: Allele 2"]].max(axis=1)

    OPTIONAL_COLUMNS = [
        "RepeatUnit",
        "VariantId",

        args.sample_analysis_status_column,
        args.sample_phenotypes_column,

        "VariantCatalog_Inheritance",
        "VariantCatalog_Diseases",

        "Genotype",
        "GenotypeConfidenceInterval",
    ]

    # fill in values for missing optional columns
    missing_optional_columns = set(OPTIONAL_COLUMNS) - set(df.columns)
    if missing_optional_columns:
        print(f"WARNING: {args.combined_tsv_path} is missing these columns: {missing_optional_columns}. "
              f"Filling them with None...")
        for c in missing_optional_columns:
            df.loc[:, c] = None

    for integer_column in ("CI end: Allele 1", "CI end: Allele 2"):
        if integer_column in df.columns:
            df.loc[:, integer_column] = pd.to_numeric(df[integer_column], errors="coerce")

    # sort
    df = df.sort_values(
        by=["Num Repeats: Allele 2", "Num Repeats: Allele 1", args.sample_affected_status_column],
        ascending=[False, False, False])

    df.columns = [
        normalize_column_name(c) for c in df.columns
    ]

    return df


def print_results_for_locus(args, locus_id, locus_df, threshold_lookup_by_motif=None):
    """Prints the sorted table of results for a single locus"""

    # Get the 1st row and use it to look up metadata values which are the same across all rows for the locus
    # (ie. Inheritance Mode)
    first_row = locus_df.iloc[0].to_dict()

    reference_region = first_row["ReferenceRegion"]
    motif = first_row.get(args.motif_column)
    canonical_motif = compute_canonical_motif(motif)
    locus_description = f"{locus_id} ({reference_region})"
    #if not inheritance_mode or pd.isna(inheritance_mode):
    #    if motif and compute_canonical_motif(motif) == "AAG":
    #        inheritance_mode = "XR" if "X" in reference_region else "AR"
    #    else:
    #        inheritance_mode = "AD"

    # create a list of dfs that are subsets of locus_df and where rows pass thresholds
    dfs_to_process = []

    # create a list of dfs to print, filtered by the pathogenic thresholds and/or affected status
    inheritance_mode = args.inheritance_mode
    threshold = None
    if args.threshold:
        threshold = args.threshold

    elif args.use_thresholds:
        if canonical_motif in threshold_lookup_by_motif:
            threshold, inheritance_mode = threshold_lookup_by_motif[canonical_motif]
            print(f"Filtering by pathogenic threshold >= {threshold} x {motif} repeats")
        else:
            print(f"WARNING: No pathogenic thresholds found for canonical motif {canonical_motif} @ locus {locus_id}. Skipping...")
            return None
    else:
        dfs_to_process.append(locus_df)

    if threshold is not None:
        if inheritance_mode == "XR":

            # split results by male/female
            male_df = locus_df[~locus_df["Genotype"].str.contains("/") | locus_df["is_male"]]
            male_df = male_df[
                (male_df[normalize_column_name(f"{args.number_to_threshold}: Allele 1")] >= threshold) |
                (male_df[normalize_column_name(f"{args.number_to_threshold}: Allele 2")] >= threshold)
            ].iloc[0:args.max_rows]
            #male_df = pd.concat([threshold_records_for_male_samples, male_df], ignore_index=True)
            dfs_to_process.append(male_df)

            female_df = locus_df[locus_df["Genotype"].str.contains("/") | ~locus_df["is_male"]]
            female_df = female_df[
                (female_df[normalize_column_name(f"{args.number_to_threshold}: Allele 1")] >= threshold) &
                (female_df[normalize_column_name(f"{args.number_to_threshold}: Allele 2")] >= threshold)
            ].iloc[0:args.max_rows]

            #female_df = pd.concat([threshold_records, female_df], ignore_index=True)
            dfs_to_process.append(female_df)

        else:
            if inheritance_mode == "AR":
                locus_df = locus_df[
                    (locus_df[normalize_column_name(f"{args.number_to_threshold}: Allele 1")] >= threshold) &
                    (locus_df[normalize_column_name(f"{args.number_to_threshold}: Allele 2")] >= threshold)
                ].iloc[0:args.max_rows]
            else:
                # for x-linked dominant, it's enough for one of the alleles to be above the threshold
                # (ie. male vs. female genotyes)
                locus_df = locus_df[
                    (locus_df[normalize_column_name(f"{args.number_to_threshold}: Allele 1")] >= threshold) |
                    (locus_df[normalize_column_name(f"{args.number_to_threshold}: Allele 2")] >= threshold)
                ].iloc[0:args.max_rows]

            #locus_df = pd.concat([threshold_records, locus_df], ignore_index=True)
            dfs_to_process.append(locus_df)

    if args.use_affected:
        # filter by affected status
        filtered_dfs_list = []
        for df_to_process in dfs_to_process:
            ingored_unaffected_counter = 0
            affected_counter = 0
            first_non_ignored_unaffected_allele_size = None
            first_affected_allele_size = None
            last_affected_allele_size = None
            idx = 0
            for row in df_to_process.itertuples(index=False):
                row = row._asdict()
                #print(row)
                affected_status = row[normalize_column_name(args.sample_affected_status_column)]
                idx += 1
                if not affected_status:
                    continue

                if affected_status == "Not Affected":
                    ingored_unaffected_counter += 1
                    first_non_ignored_unaffected_allele_size = int(row[normalize_column_name(f"{args.number_to_threshold}: Allele 2")])
                    if ingored_unaffected_counter > args.max_n_unaffected:
                        break

                elif affected_status == "Affected":
                    affected_allele_size = int(row[normalize_column_name(f"{args.number_to_threshold}: Allele 2")])
                    affected_counter += 1
                    if not first_affected_allele_size:
                        first_affected_allele_size = affected_allele_size
                    last_affected_allele_size = affected_allele_size

                elif affected_status == "Unknown":
                    pass
                else:
                    print(f"WARNING: unexpected affected status: {affected_status}")

            if affected_counter == 0:
                continue

            if last_affected_allele_size > 300:
                min_separation_between_affected_and_unaffected = 100
            elif last_affected_allele_size > 200:
                min_separation_between_affected_and_unaffected = 50
            elif last_affected_allele_size > 120:
                min_separation_between_affected_and_unaffected = 20
            elif last_affected_allele_size > 75:
                min_separation_between_affected_and_unaffected = 10
            elif last_affected_allele_size > 50:
                min_separation_between_affected_and_unaffected = 5
            else:
                min_separation_between_affected_and_unaffected = 2

            if first_non_ignored_unaffected_allele_size and last_affected_allele_size < first_non_ignored_unaffected_allele_size + min_separation_between_affected_and_unaffected:
                continue

            df_subset = df_to_process.iloc[:idx + 3]
            if len(df_subset) == len(df_to_process):
                print("----- all rows included")

            filtered_dfs_list.append(df_subset)
        dfs_to_process = filtered_dfs_list

    for i, df_to_process in enumerate(dfs_to_process):
        if len(df_to_process) == 0:
            continue

        print(f"Found {len(df_to_process)} samples passed filters in table {i+1} out of "
              f"{len(dfs_to_process)} for locus {locus_id}")


        # Print the filtered results for this locus
        columns = [
            args.sample_id_column,
            "LocusId",

            args.sample_affected_status_column,
            args.sample_sex_column,

            "Genotype",
            "GenotypeConfidenceInterval",

            "VariantCatalog_Inheritance",
            "RepeatUnit",

            args.sample_analysis_status_column,
            args.sample_phenotypes_column,
            args.sample_paternal_id_column,
            args.sample_maternal_id_column,
        ]
        if "PaternalGenotype" in df_to_process.columns:   columns += ["PaternalGenotype"]
        if "MaternalGenotype" in df_to_process.columns:   columns += ["MaternalGenotype"]


        df_to_process = df_to_process[columns]

        # Shorten some column names so more can fit on screen
        df_to_process.rename(columns={
            args.sample_affected_status_column: "affected",
            args.sample_sex_column: "sex",
            args.sample_analysis_status_column: "analysis_status",
            args.sample_phenotypes_column: "hpo",
            "GenotypeConfidenceInterval": "GenotypeCI",
            "VariantCatalog_Inheritance": "Mode",
            "RepeatUnit": "Motif",
        }, inplace=True)

        if len(df_to_process) > 0:
            print("="*100)  # print a divider
            print(f"** {locus_id} **")
            print("**Locus**: ", locus_description)
            print("**Inheritance**: ", inheritance_mode)
            if threshold:
                print("**Pathogenic Threshold**: >=", threshold, "x", motif)

        # Print the candidate pathogenic rows for this locus
        print(tabulate(df_to_process, headers="keys", tablefmt="psql", showindex=range(1, len(df_to_process)+1)))

    if dfs_to_process:
        return pd.concat(dfs_to_process)
    else:
        return None


def main():
    args = parse_args()

    # print args
    threshold_lookup_by_motif = None
    if args.known_pathogenic_variant_catalog and args.use_thresholds:
        threshold_lookup_by_motif = compute_threshold_lookup_by_motif(args.known_pathogenic_variant_catalog)

    df = load_results_table(args)

    # print example values
    print("Settings:")
    for k, v in vars(args).items():
        print(f"  {k:50s}: {v}")
    current_row = None
    max_values = 0
    for i, (_, row) in enumerate(df.iterrows()):
        non_null_values_count = sum(1 for c in df.columns if not pd.isna(row[c]))
        if non_null_values_count > max_values:
            max_values = non_null_values_count
            current_row = row

        if i > 100:
            print("\nExample row:")
            for key, value in current_row.to_dict().items():
                print(f"  {key:30s}: {value}")
            break

    print(f"Analyzing {len(set(df.LocusId)):,d} locus ids")
    variant_ids_that_are_not_locus_ids = set(df.VariantId) - set(df.LocusId)
    if variant_ids_that_are_not_locus_ids:
        print("WARNING: discarding records with VariantIds:", ", ".join(variant_ids_that_are_not_locus_ids))
        df = df[~df.VariantId.isin(variant_ids_that_are_not_locus_ids)]

    # Process each locus
    results_dfs = []
    for j, (locus_id, locus_df) in enumerate(df.groupby("LocusId")):
        print(f"Processing locus #{j+1}: {locus_id} ({len(locus_df)} non-ref genotypes)")
        results_df = print_results_for_locus(args, locus_id, locus_df, threshold_lookup_by_motif)

        if results_df is not None:
            print(f"Found {len(results_df):,d} results for locus {locus_id}")
            results_dfs.append(results_df)

    if not results_dfs:
        print("No rows passed filters")
        return
    final_results_df = pd.concat(results_dfs)
    output_path = args.output_prefix
    if args.motif:
        output_path += f".{args.motif}"
    if args.min_motif_size:
        output_path += f".{args.min_motif_size}bp_min_motif_size"
    if args.threshold:
        output_path += f".{args.threshold}_or_more_repeats"
    if args.inheritance_mode:
        output_path += f".{args.inheritance_mode}_inheritance"
    if args.purity_threshold:
        output_path += f".purity_{args.purity_threshold}"
    if args.use_thresholds:
        output_path += ".using_known_pathogenic_thresholds"
    output_path += ".tsv"

    final_results_df.to_csv(output_path, index=False, header=True, sep="\t")
    print(f"Wrote {len(final_results_df)} rows to {output_path}")

    #for locus_id, count in sorted(dict(final_results_df.groupby("LocusId").count()[args.sample_id_column]).items()):
    #   print(f"{count:10,d}  {locus_id} rows")


if __name__ == "__main__":
    main()
