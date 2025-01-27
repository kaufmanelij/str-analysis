import argparse
import binascii
import intervaltree
import os
import re
import pysam
import pandas as pd
import collections
import tempfile
import hashlib

from google.cloud import storage

from str_analysis.utils.file_utils import set_requester_pays_project, file_exists, open_file
from str_analysis.utils.misc_utils import parse_interval
from str_analysis.utils.cram_bam_utils import IntervalReader
pysam.set_verbosity(0)

def main():
    parser = argparse.ArgumentParser(description="Retrieve reads from a CRAM or BAM file that overlap one or more "
                                                 "genomic intervals, or are unmapped.")
    parser.add_argument("-u", "--gcloud-project", help="Google Cloud project to use for GCS requester pays buckets.")
    parser.add_argument("--read-index", help="Optional path of the input BAM or CRAM index file. This can be a local or a gs:// path")
    parser.add_argument("-R", "--reference-fasta", help="Optional reference genome FASTA file used when reading the CRAM file")
    parser.add_argument("-L", "--interval", action="append", default=[], help="The script will output aligned reads that "
                        "overlap the given genomic interval(s). The value can be the path of a .bed file, .bed.gz file, "
                        ".interval_list file, an interval in the format \"chr:start-end\" with a 0-based start "
                        "coordinate, or a chromosome name (ie. \"chrM\"). If a read within a given interval has an "
                        "unmapped mate, the mate will be included in the output. If a mate is mapped somewhere beyond "
                        "the specified interval(s), the mate will not be included. Read pairs where both mates are "
                        "unmapped will not be included in the output unless --include-unmapped-read-pairs is specified.")
    parser.add_argument("--padding", type=int, default=0, help="Number of bases with which to pad each interval")
    parser.add_argument("--include-unmapped-read-pairs", action="store_true",
                        help="Output read pairs where both mates are unmapped. This can be specified in addition to or "
                             "instead of -L intervals.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("input_bam_or_cram", help="Input BAM or CRAM file. This can be a local or a gs:// path")
    args = parser.parse_args()

    # validate args
    if args.debug:
        args.verbose = True

    if not args.interval and not args.include_unmapped_read_pairs:
        parser.error("At least one --interval or --include-unmapped-read-pairs arg must be specified")

    set_requester_pays_project(args.gcloud_project)
    for path in args.input_bam_or_cram, args.read_index:
        if path and not file_exists(path):
            parser.error(f"{path} not found")

    reader = IntervalReader(
        args.input_bam_or_cram,
        crai_or_bai_path=args.read_index,
        include_unmapped_read_pairs=args.include_unmapped_read_pairs,
        reference_fasta_path=args.reference_fasta,
        verbose=args.verbose,
        debug=args.debug,
    )

    # DataFrame to store results
    results = []

    # process intervals
    for interval in args.interval:
        if interval.endswith(".bed") or interval.endswith(".bed.gz") or interval.endswith(".interval_list"):
            if not file_exists(interval):
                parser.error(f"{interval} file not found")

            with open_file(interval, is_text_file=True) as f:
                for line in f:
                    if line.startswith("@"):
                        continue
                    fields = line.strip().split("\t")
                    if len(fields) < 3:
                        parser.error(f"Expected at least 3 columns in line {line}")
                    chrom, start, end = fields[:3]
                    start_offset = 1 if interval.endswith(".interval_list") else 0
                    start = int(start) - start_offset  # convert to 0-based coordinates
                    end = int(end)
                    if start > end:
                        parser.error(f"start coordinate {start} is greater than the end coordinate {end}")
                    reader.add_interval(chrom, start - args.padding, end + args.padding)
                    read_count = reader.count_reads_in_interval(chrom, start - args.padding, end + args.padding)
                    results.append({"chromosome": chrom, "start": start - args.padding, "end": end + args.padding, "read_count": read_count})
        else:
            try:
                if ":" in interval:
                    chrom, start_0based, end = parse_interval(interval)
                    if start_0based > end:
                        parser.error(f"start coordinate {start_0based} is greater than end coordinate {end}")
                    reader.add_interval(chrom, start_0based - args.padding, end + args.padding)
                    read_count = reader.count_reads_in_interval(chrom, start_0based - args.padding, end + args.padding)
                    results.append({"chromosome": chrom, "start": start_0based - args.padding, "end": end + args.padding, "read_count": read_count})
                else:
                    chrom = interval
                    reader.add_interval(chrom, 0, 10**9)
                    read_count = reader.count_reads_in_interval(chrom, 0, 10**9)
                    results.append({"chromosome": chrom, "start": 0, "end": 10**9, "read_count": read_count})
            except ValueError as e:
                parser.error(f"Invalid interval {interval}  {e}")

    # Create DataFrame from results
    df = pd.DataFrame(results)
    
    # Save DataFrame to CSV file
    df.to_csv("read_counts_per_region.csv", index=False)

    reader.close()

if __name__ == "__main__":
    main()
