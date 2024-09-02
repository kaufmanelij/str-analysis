"""
The script takes a .tsv file generated by the combine_str_json_to_tsv.py script for ExpansionHunter calls at
known disease-associated loci.
It then prints out the subset of rows that might represent pathogenic expansions and should be reviewed in more detail.

The input .tsv must have at least these input columns:

    "LocusId", "SampleId", "Sample_affected", "Sample_sex",
    "Num Repeats: Allele 1", "Num Repeats: Allele 2", "CI end: Allele 1", "CI end: Allele 2",

One way to add the  Sample_* and VariantCatalog_* columns is to run combine_str_json_to_tsv.py with the
--sample-metadata and  the --variant-catalog args. Variant catalogs can be taken from the variant_catalogs directory in
this github repo.

This script then prints out a table for each locus with sample genotypes sorted by most expanded to least expanded.
It can print all sample genotypes, or set a cut-off based on the known pathogenic threshold, or alternatively, by
print all affected individuals that are more expanded than some user-defined number of unaffected individuals.
It also takes into account locus inheritance - for example, printing 2 separate tables (males, females) for X-linked
recessive loci.
"""

import argparse
import os
import subprocess
from ast import literal_eval

import pandas as pd
from str_analysis.utils.canonical_repeat_unit import compute_canonical_motif
from tabulate import tabulate


REQUIRED_COLUMNS = [
    "LocusId",
    "Num Repeats: Allele 1",
    "Num Repeats: Allele 2",
    "CI end: Allele 1",
    "CI end: Allele 2",
]


GNOMAD_STR_TABLE_PATH = "https://gnomad-public-us-east-1.s3.amazonaws.com/release/3.1.3/tsv/gnomAD_STR_genotypes__2022_01_20.tsv.gz"

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

    #p.add_argument("--variant-catalog", help="Optional path of an ExpansionHunter variant catalog in JSON format. "
    #    "If specified, fields from the catalog will be added to the combined_tsv_table before proceeding with the analysis.")
    grp = p.add_argument_group()
    grp.add_argument("--use-affected", action="store_true", help="Use affected status to determine which samples to "
        "include in the output for each locus. Only include those affected samples that have larger expansions than "
        "the top N unaffected samples (adjustsable by --max-n-unaffected).")
    grp.add_argument("--use-thresholds", action="store_true", help="Use known pathogenic thresholds to determine which "
        "samples to include in the output for each locus. All samples with expansions above the pathogenic threshold "
        "will be included.")
    p.add_argument("-n", "--max-rows", type=int, default=10000000, help="Limit the max number of samples to include in "
        "the output for each locus.")
    p.add_argument("--max-n-unaffected", type=int, default=10, help="After this many unaffected samples are "
        "encountered with genotypes above a particular expansion size at locus, all samples (affected or unaffected) "
        "that have smaller expansions at that locus will be ignored")
    p.add_argument("--use-gnomad", action="store_true", help="Include samples from the gnomAD v3.1 STR release"
        "@ https://gnomad.broadinstitute.org/downloads#v3-short-tandem-repeats")

    p.add_argument("-l", "--locus", action="append", help="If specified, only these locus ids will be processed")
    p.add_argument("--highlight-samples", nargs="*", help="If specified, this can be the path of a text file that "
        "contains sample ids (one per line) or just 1 or more sample ids listed on the commandline")
    p.add_argument("--truth-samples", help="If specified, this can be the path of a table that contains two columns: "
        "sample id and locus id. The script will then mark each sample at that locus to indicate that it's a truth "
        "sample")
    p.add_argument("--previously-seen-samples", help="If specified, this can be the path of a table that contains "
        "two columns: sample id and locus id. The script will then mark each sample at that locus to "
        "indicate that it's already been evaluated")
    p.add_argument("--previously-diagnosed-loci", nargs="*", help="If specified, this can be the path of a "
        "text file that contains locus ids (one per line) or just 1 or more locus ids listed on the commandline. These "
        "loci will be marked as having been previously diagnosed using short-read sequencing data.")
    p.add_argument("combined_tsv_path", nargs="+", help="Path of combined ExpansionHunter .tsv table generated by the "
        "combine_str_json_to_tsv.py script. It's assumed that combine_str_json_to_tsv.py "
        "was run with --sample-metadata and --variant-catalog args to add sample-level and locus-level metadata columns")

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
    p.add_argument("--sample-genome-version-column", default="Sample_genome_version", help="Name of the column in the "
                   "combined table that contains the genome version")

    p.add_argument("--results-path", help="Export a .tsv table of rows that pass thresholds to this path",
                   default="pathogenic_and_intermediate_results.tsv")

    # Parse and validate command-line args + read in the combined table(s) from the given command_tsv_path(s)
    args = p.parse_args()

    for combined_tsv_path in args.combined_tsv_path:
        if not os.path.isfile(combined_tsv_path):
            p.error(f"{combined_tsv_path} not found")

    if args.sample_metadata_table and not os.path.isfile(args.sample_metadata_table):
        p.error(f"{args.sample_metadata_table} not found")

    #if args.variant_catalog and not os.path.isfile(args.variant_catalog):
    #    p.error(f"{args.variant_catalog} not found")

    if not args.use_affected and not args.use_thresholds:
        p.error("Must specify --use-thresholds or --use-affected")

    return args


def load_gnomad_df():
    print("Loading gnomAD STR table...")
    gnomad_df = pd.read_table(GNOMAD_STR_TABLE_PATH, usecols=[
        "Id",
        "LocusId",
        "ReferenceRegion",
        "Motif",
        "IsAdjacentRepeat",
        "Sex",
        "Allele1",
        "Allele2",
        "Genotype",
        "GenotypeConfidenceInterval",
    ],
        dtype={
          "Id": str,
          "Sex": str,
          "Allele1": str,
          "Allele2": str,
          "GenotypeConfidenceInterval": str,
          "IsAdjacentRepeat": bool,
        },
    )
    gnomad_df = gnomad_df[~gnomad_df["IsAdjacentRepeat"]]
    print(f"Processing {len(gnomad_df)} gnomAD table rows...")
    for column_name, value in [
        ("Sample_genome_version", "38"),
        ("Sample_affected", "Not Affected"),
    ]:
        gnomad_df.loc[:, column_name] = value

    gnomad_df.loc[:, "Id"] = "gnomAD:" + gnomad_df["Id"]
    if "VariantId" in gnomad_df.columns:
        gnomad_df.loc[:, "LocusId"] = gnomad_df["VariantId"]

    gnomad_df.loc[:, ("CI1", "CI2")] = gnomad_df["GenotypeConfidenceInterval"].str.split("/", expand=True)
    gnomad_df.loc[:, "CI1"] = gnomad_df["CI1"].fillna("")
    gnomad_df.loc[:, "CI2"] = gnomad_df["CI2"].fillna("")
    gnomad_df.loc[:, ("CI start: Allele 1", "CI end: Allele 1")] = gnomad_df["CI1"].str.split("-", expand=True)
    gnomad_df.loc[:, ("CI start: Allele 2", "CI end: Allele 2")] = gnomad_df["CI2"].str.split("-", expand=True)
    gnomad_df.drop(columns=["CI1", "CI2", "CI start: Allele 1", "CI start: Allele 2"], inplace=True)
    gnomad_df.rename({
        "Id": "SampleId",
        "Sex": "Sample_sex",
        "Allele1": "Num Repeats: Allele 1",
        "Allele2": "Num Repeats: Allele 2",
        "Motif": "RepeatUnit",
    }, axis="columns", inplace=True)

    return gnomad_df


def load_results_tables(args):
    all_dfs = []

    sample_metadata_df = None
    if args.sample_metadata_table:
        sample_metadata_df = pd.read_table(args.sample_metadata_table, dtype=str)
        if args.metadata_table_sample_id_column not in sample_metadata_df.columns:
            raise ValueError(f"Metadata table {args.sample_metadata_table} is missing the sample id column: "
                             f"{args.metadata_table_sample_id_column}. Use --metadata-table-sample-id-column to "
                             f"specify the correct sample id column name in this table.")

        # drop duplicates
        before = len(sample_metadata_df)
        sample_metadata_df = sample_metadata_df.drop_duplicates(subset=args.metadata_table_sample_id_column)
        if len(sample_metadata_df) != before:
            print(f"Dropped {before - len(sample_metadata_df):,d} duplicate rows ({(before - len(sample_metadata_df)) / before:.1%}) "
                  f"based on {args.metadata_table_sample_id_column} from {args.sample_metadata_table}")

        sample_metadata_df.set_index(args.metadata_table_sample_id_column, inplace=True)

    for combined_tsv_path in args.combined_tsv_path:

        # read in table
        df = pd.read_table(combined_tsv_path, low_memory=False, dtype=str)
        if sample_metadata_df is not None:
            len_before_join = len(df)

            df = df.set_index(args.sample_id_column).join(sample_metadata_df, how="left").reset_index()
            if len_before_join != len(df):
                raise ValueError(f"Sample metadata table join error: expected {len_before_join:,d} rows, got {len(df):,d}")
        df.loc[:, "Num Repeats: Allele 1"] = df["Num Repeats: Allele 1"].fillna(0).astype(float).astype(int)
        df.loc[:, "Num Repeats: Allele 2"] = df["Num Repeats: Allele 2"].fillna(0).astype(float).astype(int)

        df.loc[:, "Num Repeats: Min Allele 1, 2"] = df.apply(lambda row: (
            min(row["Num Repeats: Allele 1"], row["Num Repeats: Allele 2"])
            if
            row["Num Repeats: Allele 1"] > 0 and row["Num Repeats: Allele 2"] > 0
            else
            max(row["Num Repeats: Allele 1"], row["Num Repeats: Allele 2"])
        ), axis=1)

        df.loc[:, "Num Repeats: Max Allele 1, 2"] = df[["Num Repeats: Allele 1", "Num Repeats: Allele 2"]].max(axis=1)

        # check that all required columns are present
        missing_required_columns = (
            set(REQUIRED_COLUMNS) | {args.sample_sex_column, args.sample_affected_status_column}
        ) - set(df.columns)

        if missing_required_columns:
            raise ValueError(f"{combined_tsv_path} is missing these required columns: {missing_required_columns}")


        OPTIONAL_COLUMNS = [
            "RepeatUnit",
            "VariantId",

            args.sample_analysis_status_column,
            args.sample_phenotypes_column,
            "Sample_genome_version",

            "VariantCatalog_Inheritance",
            "VariantCatalog_Diseases",

            "Genotype",
            "GenotypeConfidenceInterval",
        ]

        # fill in values for missing optional columns
        missing_optional_columns = set(OPTIONAL_COLUMNS) - set(df.columns)
        if missing_optional_columns:
            print(f"WARNING: {combined_tsv_path} is missing these columns: {missing_optional_columns}. "
                  f"Filling them with None...")
            for c in missing_optional_columns:
                df.loc[:, c] = None

        for integer_column in ("CI end: Allele 1", "CI end: Allele 2"):
            df.loc[:, integer_column] = pd.to_numeric(df[integer_column], errors="coerce")

        all_dfs.append((df, combined_tsv_path))

    return all_dfs


def print_results_for_locus(args, locus_id, locus_df, highlight_locus=False):
    """Prints the sorted table of results for a single locus"""

    for column_name in (
            args.sample_affected_status_column, args.sample_sex_column, args.sample_analysis_status_column,
            args.sample_genome_version_column,
    ):
        # split values like a; b; b; and collapse to a; b
        locus_df.loc[:, column_name] = locus_df[column_name].apply(
            lambda s: "; ".join(set(s.split("; "))) if not pd.isna(s) else s)
        # truncate long text so it fits on screen
        locus_df.loc[:, column_name] = locus_df[column_name].str[0:50]

    locus_df[args.sample_affected_status_column] = locus_df[args.sample_affected_status_column].replace({
        "Unaffected": "Not Affected",
        "Possibly Affected": "Unknown",
    })

    unexpected_affected_status_values = set(locus_df[
        ~locus_df[args.sample_affected_status_column].isin({"Affected", "Not Affected", "Unknown"})
    ][args.sample_affected_status_column])
    if unexpected_affected_status_values:
        print(f"WARNING: Some rows have unexpected affected value(s): {unexpected_affected_status_values}")

    # Sort
    locus_df = locus_df.sort_values(
        by=["Num Repeats: Allele 2", "Num Repeats: Allele 1", args.sample_affected_status_column],
        ascending=False)

    # Get the 1st row and use it to look up metadata values which are the same across all rows for the locus
    # (ie. Inheritance Mode)
    first_row = locus_df.iloc[0].to_dict()

    disease_info = first_row.get("VariantCatalog_Diseases")
    intermediate_threshold_min = None
    pathogenic_threshold_min = None
    if disease_info and not pd.isna(disease_info):
        try:
            disease_info = literal_eval(disease_info)
        except Exception as e:
            raise ValueError(f"Unable to parse {disease_info} as json: {e}")

        try:
            intermediate_threshold_min = min(int(float(d["NormalMax"]) + 1) for d in disease_info if d.get("NormalMax"))
        except ValueError as e:
            print(f"WARNING: {locus_id} couldn't parse NormalMax fields from disease_info {disease_info}: {e}")

        try:
            pathogenic_threshold_min = min(int(float(d["PathogenicMin"])) for d in disease_info if d.get("PathogenicMin"))
        except ValueError as e:
            print(f"WARNING: {locus_id} couldn't parse PathogenicMin fields from disease_info {disease_info}: {e}")

    reference_region = first_row["ReferenceRegion"]
    genome_version = f"GRCh{first_row[args.sample_genome_version_column]}" if first_row.get(args.sample_genome_version_column) else ""
    motif = first_row.get("RepeatUnit")
    locus_description = f"{locus_id} ({reference_region}: {genome_version})  https://stripy.org/database/{locus_id}"
    inheritance_mode = first_row.get("VariantCatalog_Inheritance")
    if not inheritance_mode or pd.isna(inheritance_mode):
        inheritance_mode = "XR" if "X" in reference_region else "AD"

    if highlight_locus:
        locus_description += " " * 100 + "  <== previously diagnosed using short reads"
    print("**Locus**: ", locus_description)
    print("**Disease**: ", str(disease_info))
    print("**Inheritance**: ", inheritance_mode)
    print("**Pathogenic Threshold**: >=", pathogenic_threshold_min, "x", motif)
    print("**Intermediate Threshold**: >=", intermediate_threshold_min, "x", motif)

    # replace NA with "Unknown" strings
    locus_df.loc[:, args.sample_affected_status_column] = locus_df[args.sample_affected_status_column].fillna("Unknown")
    locus_df.loc[:, "is_male"] = locus_df[args.sample_sex_column].str.lower().str.startswith("m")  # this leaves missing values as Na
    locus_df.loc[:, "is_female"] = locus_df[args.sample_sex_column].str.lower().str.startswith("f")  # this leaves missing values as Na
    locus_df.loc[:, args.sample_sex_column] = locus_df[args.sample_sex_column].fillna("Unknown")
    # create a list of dfs that are subsets of locus_df and where rows pass thresholds
    dfs_to_process = []
    if locus_id == "COMP":
        # COMP is a special case where contractions below 5 repeats are also pathogenic
        df_comp = locus_df[
            (locus_df["CI end: Allele 1"] < 5) |
            (locus_df["CI end: Allele 2"] < 5)
        ]
        dfs_to_process.append(df_comp)

    # add 2 rows to each table representing the pathogenic thresholds
    threshold_records = []
    threshold_records_for_male_samples = []
    for label, threshold in [
        ("Intermediate Threshold", intermediate_threshold_min),
        ("Pathogenic Threshold",   pathogenic_threshold_min),
    ]:
        if not threshold: continue

        threshold_record = {c: SEPARATOR_STRING for c in locus_df.columns}
        threshold_record.update({
            "SampleId": f"**{label}**", "LocusId": locus_id, "VariantId": locus_id,
            "Genotype": f">= {threshold}",
            "Num Repeats: Min Allele 1, 2": threshold,
            "Num Repeats: Max Allele 1, 2": threshold,
            "Num Repeats: Allele 1": threshold,
            "Num Repeats: Allele 2": 0,
        })

        threshold_records_for_male_samples.append(dict(threshold_record))

        threshold_record.update({
            "Num Repeats: Allele 2": threshold,
        })
        threshold_records.append(dict(threshold_record))

    threshold_records = pd.DataFrame(threshold_records)
    threshold_records_for_male_samples = pd.DataFrame(threshold_records_for_male_samples)
    args.max_rows += 2  # add 2 to allow for these threshold rows

    # create a list of dfs to print, filtered by the pathogenic thresholds and/or affected status
    threshold = min(list(filter(None, [intermediate_threshold_min, pathogenic_threshold_min])) or [0]) if args.use_thresholds else 0

    use_thresholds = False
    if args.use_thresholds:
        if threshold > 0:
            use_thresholds = True
            print(f"Filtering by pathogenic thresholds: >= {threshold} repeats")
        else:
            print(f"WARNING: No pathogenic thresholds found for locus {locus_id}. Skipping threshold filtering")

    if not use_thresholds:
        dfs_to_process.append(locus_df)

    elif use_thresholds:
        if inheritance_mode == "XR":

            # split results by male/female
            male_df = locus_df[~locus_df["Genotype"].str.contains("/") | locus_df["is_male"]]
            male_df = male_df[
                (male_df["CI end: Allele 1"] >= threshold)
            ].iloc[0:args.max_rows]
            male_df = pd.concat([threshold_records_for_male_samples, male_df], ignore_index=True)
            dfs_to_process.append(male_df)

            female_df = locus_df[locus_df["Genotype"].str.contains("/") | locus_df["is_female"]]
            female_df = female_df[
                (female_df["CI end: Allele 1"] >= threshold) &
                (female_df["CI end: Allele 2"] >= threshold)
            ].iloc[0:args.max_rows]

            female_df = pd.concat([threshold_records, female_df], ignore_index=True)
            dfs_to_process.append(female_df)

        else:
            if inheritance_mode == "AR":
                locus_df = locus_df[
                    (locus_df["CI end: Allele 1"] >= threshold) &
                    (locus_df["CI end: Allele 2"] >= threshold)
                ].iloc[0:args.max_rows]
            else:
                # for x-linked dominant, it's enough for one of the alleles to be above the threshold
                # (ie. male vs. female genotyes)
                locus_df = locus_df[
                    (locus_df["CI end: Allele 1"] >= threshold) |
                    (locus_df["CI end: Allele 2"] >= threshold)
                ].iloc[0:args.max_rows]

            locus_df = pd.concat([threshold_records, locus_df], ignore_index=True)
            dfs_to_process.append(locus_df)

    if args.use_affected:
        # filter by affected status
        filtered_dfs_list = []
        for df_to_process in dfs_to_process:
            unaffected_counter = 0
            idx = 0
            for affected_status in df_to_process[args.sample_affected_status_column]:
                idx += 1
                if affected_status and ("Not Affected" in affected_status or "Unknown" in affected_status):
                    unaffected_counter += 1

                if unaffected_counter >= args.max_n_unaffected:
                    break

            df_to_process = df_to_process.iloc[:idx]

            filtered_dfs_list.append(df_to_process)
        dfs_to_process = filtered_dfs_list

    for i, df_to_process in enumerate(dfs_to_process):
        print(f"Found {len(df_to_process)} samples passed filters in table {i+1} out of "
              f"{len(dfs_to_process)} for locus {locus_id}")

        if len(df_to_process) == 0:
            continue

        if inheritance_mode in {"AR", "XR"}:
            df_to_process = df_to_process.sort_values(
                by=["Num Repeats: Min Allele 1, 2", args.sample_affected_status_column],
                ascending=False)
        elif inheritance_mode in {"AD", "XD", "Unknown"}:
            df_to_process = df_to_process.sort_values(
                by=["Num Repeats: Max Allele 1, 2", args.sample_affected_status_column],
                ascending=False)
        else:
            raise ValueError(f"Unexpected inheritance mode: {inheritance_mode}")

        # Print the filtered results for this locus
        df_to_process = df_to_process[[
            args.sample_id_column,
            "LocusId",

            args.sample_affected_status_column,
            args.sample_sex_column,
            args.sample_genome_version_column,

            "Genotype",
            "GenotypeConfidenceInterval",

            "VariantCatalog_Inheritance",
            "RepeatUnit",

            args.sample_analysis_status_column,
            args.sample_phenotypes_column,
        ]]

        # Shorten some column names so more can fit on screen
        df_to_process.rename(columns={
            args.sample_affected_status_column: "affected",
            args.sample_sex_column: "sex",
            args.sample_analysis_status_column: "analysis_status",
            args.sample_phenotypes_column: "hpo",
            args.sample_genome_version_column: "genome",
            "GenotypeConfidenceInterval": "GenotypeCI",
            "VariantCatalog_Inheritance": "Mode",
            "RepeatUnit": "Motif",
        }, inplace=True)

        # Print the candidate pathogenic rows for this locus
        print(tabulate(df_to_process, headers="keys", tablefmt="psql", showindex=False))

    return pd.concat(dfs_to_process)


def main():
    args = parse_args()

    all_dfs = load_results_tables(args)

    all_sample_ids = set()
    all_locus_ids = set()
    for df, _ in all_dfs:
        all_locus_ids |= set(df.LocusId)
        all_sample_ids |= set(df[args.sample_id_column])

    print(f"Analyzing {len(all_locus_ids):,d} locus ids")
    all_variant_ids = set()
    for df, _ in all_dfs:
        all_variant_ids |= set(df.VariantId)

    variant_ids_that_are_not_locus_ids = all_variant_ids - all_locus_ids
    if variant_ids_that_are_not_locus_ids:
        print("WARNING: discarding records with VariantIds:", ", ".join(variant_ids_that_are_not_locus_ids))
        for i, (df, df_source_path) in enumerate(all_dfs):
            df = df[~df.VariantId.isin(variant_ids_that_are_not_locus_ids)]
            all_dfs[i] = (df, df_source_path)

    if args.use_gnomad:
        gnomad_df = load_gnomad_df()

        for i, df in enumerate(all_dfs):
            print("gnomad_df columns: ", gnomad_df.columns)
            print("df columns: ", df.columns)
            df_with_gnomad = pd.concat([df, gnomad_df])
            df_with_gnomad.fillna("", inplace=True)
            all_dfs[i] = df_with_gnomad

    previously_diagnosed_loci = set()
    if args.previously_diagnosed_loci:
        for l in args.previously_diagnosed_loci:
            if not os.path.isfile(l):
                previously_diagnosed_loci.add(l)
            else:
                with open(l, "rt") as f:
                    for i, s in enumerate(f.readlines()):
                        previously_diagnosed_loci.add(s.strip())

                print(f"Read {i + 1} sample ids to highlight from: {l}")

        if previously_diagnosed_loci - all_locus_ids:
            print(f"WARNING: cannot highlight {len(previously_diagnosed_loci - all_locus_ids)} loci as they aren't in the "
                  f"main table(s): {previously_diagnosed_loci - all_locus_ids}")

        previously_diagnosed_loci = previously_diagnosed_loci & all_locus_ids
        print(f"Will highlight {len(previously_diagnosed_loci)} locus ids: {previously_diagnosed_loci}")

    highlight_sample_ids = set()
    if args.highlight_samples:
        for h in args.highlight_samples:
            if not os.path.isfile(h):
                highlight_sample_ids.add(h)
            else:
                with open(h, "rt") as f:
                    for i, s in enumerate(f.readlines()):
                        highlight_sample_ids.add(s.strip())

                print(f"Read {i + 1} sample ids to highlight from: {h}")

        if highlight_sample_ids - all_sample_ids:
            print(f"WARNING: cannot highlight {len(highlight_sample_ids - all_sample_ids)} ids as they aren't in the "
                  f"main table(s): {highlight_sample_ids - all_sample_ids}")

        highlight_sample_ids = highlight_sample_ids & all_sample_ids
        print(f"Will highlight {len(highlight_sample_ids)} sample ids: {highlight_sample_ids}")

    if args.truth_samples:
        truth_df = pd.read_table(args.truth_samples, names=["sample_id", "locus_id"] + [f"c{i}" for i in range(3, 10)])

        if set(truth_df.sample_id) - all_sample_ids:
            print(f"WARNING: cannot mark {len(set(truth_df.sample_id) - all_sample_ids)} samples ids from the truth "
                  f"samples table since they aren't in the main table(s): {set(truth_df.sample_id) - all_sample_ids}")
        if set(truth_df.locus_id) - all_locus_ids:
            print(f"WARNING: cannot mark {len(set(truth_df.locus_id) - all_locus_ids)} loci from the truth samples "
                  f"table since they aren't in the main table(s): {set(truth_df.locus_id) - all_locus_ids}")

    if args.previously_seen_samples:
        previously_seen_samples_df = pd.read_table(args.previously_seen_samples,
                                                   names=["sample_id", "locus_id"] + [f"c{i}" for i in range(3, 10)])

        if set(previously_seen_samples_df.sample_id) - all_sample_ids:
            print(f"WARNING: cannot mark {len(set(previously_seen_samples_df.sample_id) - all_sample_ids)} samples ids "
                  f"from the previously seen samples table since they aren't in the main table(s): "
                  f"{set(previously_seen_samples_df.sample_id) - all_sample_ids}")
        if set(previously_seen_samples_df.locus_id) - all_locus_ids:
            print(f"WARNING: cannot mark {len(set(previously_seen_samples_df.locus_id) - all_locus_ids)} loci from the "
                  f"previously seen sample table since they aren't in the main table(s): "
                  f"{set(previously_seen_samples_df.locus_id) - all_locus_ids}")

    # Process each locus
    results_dfs = []
    for locus_id in sorted(all_locus_ids):
        if args.locus and locus_id not in args.locus:
            continue

        # Filter to rows for the current locus
        for i, (full_df, df_source_path) in enumerate(all_dfs):
            print("="*100)  # print a divider
            print(f"** {locus_id} from {df_source_path} **")
            locus_df = full_df[full_df.LocusId == locus_id].copy()

            print(f"Found {len(locus_df):,d} rows for locus {locus_id}")
            if len(locus_df) == 0:
                continue

            if highlight_sample_ids:
                locus_df.loc[:, args.sample_id_column] = locus_df[args.sample_id_column].apply(
                    lambda s: (s if s not in highlight_sample_ids else f"==> {s}"))

            truth_samples_for_this_locus = set()
            if args.truth_samples and locus_id in set(truth_df.locus_id):
                truth_samples_for_this_locus = set(
                    truth_df[truth_df.locus_id == locus_id].sample_id)

            previously_seen_samples_for_this_locus = set()
            if args.previously_seen_samples and locus_id in set(previously_seen_samples_df.locus_id):
                previously_seen_samples_for_this_locus = set(
                    previously_seen_samples_df[previously_seen_samples_df.locus_id == locus_id].sample_id)

            locus_df.loc[:, args.sample_id_column] = locus_df[args.sample_id_column].apply(
                lambda s: (
                    f"*T* {s}" if s in truth_samples_for_this_locus else (
                    f"*P* {s}" if s in previously_seen_samples_for_this_locus else s)
                )
            )

            results_df = print_results_for_locus(
                args, locus_id, locus_df, highlight_locus=locus_id in previously_diagnosed_loci)

            results_dfs.append(results_df)

    final_results_df = pd.concat(results_dfs)
    final_results_df = final_results_df[final_results_df[args.sample_affected_status_column] != SEPARATOR_STRING]
    final_results_df.to_csv(args.results_path, index=False, header=True, sep="\t")
    print(f"Wrote {len(final_results_df)} rows to {args.results_path}")
    for locus_id, count in sorted(dict(final_results_df.groupby("LocusId").count()[args.sample_id_column]).items()):
        print(f"{count:10,d}  {locus_id} rows")


if __name__ == "__main__":
    main()
