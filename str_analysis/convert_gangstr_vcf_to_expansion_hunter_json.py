"""This script converts a GangSTR output VCF to the .json format ExpansionHunter uses to output results.
This makes it easier to pass GangSTR results to downstream scripts.
"""

"""
GangSTR output vcf format:

chr14   
39042968        
.       
tttatttatttatttattta    
.       
.       
.       
END=39042990;RU=ttta;PERIOD=4;REF=5;GRID=2,8;STUTTERUP=0.05;STUTTERDOWN=0.05;STUTTERP=0.9;EXPTHRESH=-1  
GT:DP:Q:REPCN:REPCI:RC:ENCLREADS:FLNKREADS:ML:INS:STDERR:QEXP   
0/0:18:0.893606:5,5:5-5,5-5:7,11,0,0:5,7:NULL:124.576:570.176,153.445:0,0:-1,-1,-1
"""

"""
ExpansionHunter output format:

  "LocusResults": {
        "chr12-57610122-57610131-GCA": {
          "AlleleCount": 2,
          "Coverage": 50.469442942130875,
          "FragmentLength": 433,
          "LocusId": "chr12-57610122-57610131-GCA",
          "ReadLength": 151,
          "Variants": {
            "chr12-57610122-57610131-GCA": {
              "CountsOfFlankingReads": "(1, 1), (2, 4)",
              "CountsOfInrepeatReads": "()",
              "CountsOfSpanningReads": "(2, 1), (3, 48), (6, 1)",
              "Genotype": "3/3",
              "GenotypeConfidenceInterval": "3-3/3-3",
              "ReferenceRegion": "chr12:57610122-57610131",
              "RepeatUnit": "GCA",
              "VariantId": "chr12-57610122-57610131-GCA",
              "VariantSubtype": "Repeat",
              "VariantType": "Repeat"
            }
          }
        },

  "SampleParameters": {
        "SampleId": "NA19239",
        "Sex": "Female"
  }
"""


import argparse
import json
import os
import re


def main():
    p = argparse.ArgumentParser()
    p.add_argument("vcf_path", nargs="+", help="GangSTR vcf path(s)")
    args = p.parse_args()

    for vcf_path in args.vcf_path:
        print(f"Processing {vcf_path}")
        locus_results = process_gangstr_vcf(vcf_path)

        output_json_path = vcf_path.replace(".vcf", "").replace(".gz", "") + ".json"
        print(f"Writing results for", len(locus_results["LocusResults"]), f"loci to {output_json_path}")
        with open(output_json_path, "wt") as f:
            json.dump(locus_results, f, indent=3)


def process_gangstr_vcf(vcf_path):
    sample_id = os.path.basename(vcf_path).replace("gangstr.", "").strip(".")
    locus_results = {
        "LocusResults": {},
        "SampleParameters": {
            "SampleId": sample_id,
            "Sex": None,
        },
    }

    with open(vcf_path, "rt") as vcf:
        line_counter = 0
        for line in vcf:
            if line.startswith("#"):
                if line.startswith("##command="):
                    # Parse the "--bam-samps RGP_1126_1 --samp-sex F" args from the command
                    sample_id_match = re.search("bam-samps ([^-]+)", line)
                    if sample_id_match:
                        locus_results["SampleParameters"]["SampleId"] = sample_id_match.group(1).strip()
                    sample_sex_match = re.search("samp-sex ([^- ]+)", line)
                    if sample_sex_match:
                        sample_sex = sample_sex_match.group(1).strip()
                        locus_results["SampleParameters"]["Sex"] = "Female" if sample_sex.upper().startswith("F") else ("Male" if sample_sex.upper().startswith("M") else None)
                elif line.startswith("#CHROM"):
                    header_fields = line.strip().split("\t")
                    if len(header_fields) == 10:
                        locus_results["SampleParameters"]["SampleId"] = header_fields[9]

                continue

            line_counter += 1
            fields = line.strip().split("\t")
            chrom = fields[0]
            start_1based = fields[1]
            #ref = fields[3]
            #alt = fields[4]
            info = fields[7]
            if not fields[9] or fields[9] == ".":  # no genotype
                continue

            info_dict = dict([key_value.split("=") for key_value in info.split(";")])
            genotype_fields = fields[8].split(":")
            genotype_values = fields[9].split(":")
            genotype_dict = dict(zip(genotype_fields, genotype_values))

            try:
                repeat_unit = info_dict["RU"].upper()
                end = info_dict["END"]
                ref_repeat_count = int(info_dict["REF"])

                locus_id = f"{chrom}-{int(start_1based) - 1}-{end}-{repeat_unit}"
                locus_results["LocusResults"][locus_id] = {
                    "AlleleCount": genotype_dict["REPCN"].count(",") + 1,
                    "LocusId": locus_id,
                    "Coverage": float(genotype_dict["DP"]), #10.757737459978655,
                    "ReadLength": None,
                    "FragmentLength": None,
                    "Variants": {
                        locus_id: {
                            "Genotype": genotype_dict["REPCN"].replace(",", "/"), #"17/17",
                            "GenotypeConfidenceInterval": genotype_dict["REPCI"].replace(",", "/"), #"17-17/17-17",
                            "ReferenceRegion": f"{chrom}:{int(start_1based) - 1}-{end}",
                            "RepeatUnit": repeat_unit,
                            "VariantId": locus_id,
                            "VariantSubtype": "Repeat",
                            "VariantType": "Repeat",
                            #"CountsOfFlankingReads": "()",
                            #"CountsOfInrepeatReads": "()",
                            #"CountsOfSpanningReads": "()",
                        }
                    }
                }
            except Exception as e:
                print(f"Error on vcf record #{line_counter}: {e}")
                print(line)
                print(genotype_dict)

    return locus_results


if __name__ == "__main__":
    main()