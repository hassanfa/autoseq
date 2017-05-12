from pypedream.pipeline.pypedreampipeline import PypedreamPipeline
from autoseq.util.path import normpath, stripsuffix
from autoseq.tools.alignment import align_library
from autoseq.util.library import find_fastqs
from autoseq.tools.picard import PicardCollectInsertSizeMetrics, PicardCollectOxoGMetrics, \
    PicardMergeSamFiles, PicardMarkDuplicates, PicardCollectHsMetrics
from autoseq.tools.variantcalling import Freebayes, VEP, VcfAddSample, call_somatic_variants
from autoseq.tools.intervals import MsiSensor
from autoseq.tools.cnvcalling import CNVkit
from autoseq.tools.contamination import ContEst, ContEstToContamCaveat, CreateContestVCFs
from autoseq.tools.qc import *
import collections, logging


class SinglePanelResults(object):
    """
    Represents the results generated by performing analysis on a unique sample library capture,
    irrespective of the sample type.
    """
    def __init__(self):
        self.merged_bamfile = None

        # CNV kit outputs:
        self.cnr = None
        self.cns = None
        
        # Coverage QC call:
        self.cov_qc_call = None


class CancerVsNormalPanelResults(object):
    """
    Represents the results generated by performing a paired analysis comparing a cancer and a normal capture.
    """
    def __init__(self):
        self.somatic_vcf = None,
        self.msi_output = None,
        self.hzconcordance_output = None,
        self.vcf_addsample_output = None,
        self.normal_contest_output = None,
        self.cancer_contest_output = None,
        self.cancer_contam_call = None


# Fields defining a unique library capture item:
UniqueCapture = collections.namedtuple(
    'sample_type',
    'sample_id',
    'library_kit_id',
    'capture_kit_id'
)


def parse_capture_tuple(clinseq_barcode):
    """
    Convenience function for use in the context of joint panel analysis.

    Extracts the sample type, sample ID, library prep ID, and capture kit ID,
    from the specified clinseq barcode.

    :param clinseq_barcode: List of one or more clinseq barcodes 
    :return: (sample type, sample ID, capture kit ID) named tuple
    """
    return UniqueCapture(parse_sample_type(clinseq_barcode),
                         parse_sample_id(clinseq_barcode),
                         parse_prep_kit_id(clinseq_barcode),
                         parse_capture_kit_id(clinseq_barcode))


def compose_sample_str(capture):
    return "{}-{}-{}-{}".format(capture.sample_type,
                                capture.sample_id,
                                capture.library_kit_id,
                                capture.capture_kit_id)


def parse_sample_type(clinseq_barcode):
    return clinseq_barcode.split("-")[3]


def parse_sample_id(clinseq_barcode):
    return clinseq_barcode.split("-")[4]


def parse_prep_kit_id(clinseq_barcode):
    return clinseq_barcode.split("-")[5][:2]


def parse_capture_kit_id(clinseq_barcode):
    return clinseq_barcode.split("-")[6][:2]


class ClinseqPipeline(PypedreamPipeline):
    def __init__(self, sampledata, refdata, outdir, libdir, analysis_id=None, maxcores=1,
                 scratch="/scratch/tmp/tmp", **kwargs):
        PypedreamPipeline.__init__(self, normpath(outdir), **kwargs)
        self.sampledata = sampledata
        self.refdata = refdata
        self.maxcores = maxcores
        self.analysis_id = analysis_id
        self.libdir = libdir
        self.qc_files = []
        self.scratch = scratch

        # Dictionary linking unique captures to corresponding generic single panel
        # analysis results (SinglePanelResults objects as values):
        self.capture_to_results = collections.defaultdict(SinglePanelResults)

        # Dictionary linking unique normal library capture items to their corresponding
        # germline VCF filenames:
        self.normal_capture_to_vcf = {}

        # Dictionary linking (normal capture, cancer capture) pairings to corresponding
        # cancer library capture analysis results (CancerPanelResults objects as values):
        self.normal_cancer_pair_to_results = collections.defaultdict(CancerVsNormalPanelResults)

    def set_germline_vcf(self, normal_capture, vcf_filename):
        """
        Registers the specified vcf filename for the specified normal capture item,
        for this analysis.

        :param normal_capture: Normal panel capture identifier.
        :param vcf_filename: VCF filename to store.
        """

        self.normal_capture_to_vcf[normal_capture] = vcf_filename

    def get_germline_vcf(self, normal_capture):
        if normal_capture in self.normal_capture_to_vcf:
            return self.normal_capture_to_vcf[normal_capture]
        else:
            return None

    def set_capture_bam(self, unique_capture, bam):
        """
        Set the bam file corresponding to the specified unique_capture in this analysis.

        :param unique_capture: A UniqueCapture item. 
        :param bam: The bam filename.
        """
        self.capture_to_results[unique_capture].merged_bamfile = bam

    def set_capture_cnr(self, unique_capture, cnr):
        self.capture_to_results[unique_capture].cnr = cnr 

    def set_capture_cns(self, unique_capture, cns):
        self.capture_to_results[unique_capture].cns = cns
        
    def get_capture_bam(self, unique_capture):
        """
        Retrieve the bam file corresponding to the specified unique_capture in this analysis.

        :param unique_capture: A UniqueCapture item. 
        :return: The corresponding bam filename, or None if it has not been configured.
        """
        if unique_capture in self.get_all_unique_captures():
            return self.capture_to_results[unique_capture].merged_bamfile
        else:
            return None

    def check_sampledata(self):
        def check_clinseq_barcode_for_data(lib):
            if lib:
                filedir = os.path.join(self.libdir, lib)
                if not os.path.exists(filedir):
                    logging.warn("Dir {} does not exists for {}. Not using library.".format(filedir, lib))
                    return None
                if find_fastqs(lib, self.libdir) == (None, None):
                    logging.warn("No fastq files found for {} in dir {}".format(lib, filedir))
                    return None
            logging.debug("Library {} has data. Using it.".format(lib))
            return lib

        for datatype in ['panel', 'wgs']:
            for sample_type in ['N', 'T', 'CFDNA']:
                clinseq_barcodes_with_data = []
                for clinseq_barcode in self.sampledata[datatype][sample_type]:
                    barcode_checked = check_clinseq_barcode_for_data(clinseq_barcode)
                    if barcode_checked:
                        clinseq_barcodes_with_data.append(barcode_checked)

                self.sampledata[datatype][sample_type] = clinseq_barcodes_with_data

    def get_vep(self):
        vep = False
        if self.refdata['vep_dir']:
            vep = True

        return vep

    def get_all_unique_captures(self):
        """
        Obtain tuples for all unique sample library captures in this pipeline instance.
        :return: List of tuples. 
        """

        return self.capture_to_results.keys()

    def get_unique_normal_captures(self):
        """
        Obtain tuples for all unique normal sample library captures in this pipeline instance.

        :return: List of tuples.
        """

        all_unique_captures = self.get_all_unique_captures()
        return filter(lambda curr_tup: curr_tup[0] == "N", all_unique_captures)

    def get_unique_cancer_captures(self):
        """
        Obtain tuples for all unique cancer sample library captures in this pipeline instance.

        :return: List of tuples.
        """

        all_unique_captures = self.get_all_unique_captures()
        return filter(lambda curr_tup: curr_tup[0] != "N", all_unique_captures)

    def get_unique_tumor_captures(self):
        all_unique_captures = self.get_all_unique_captures()
        return filter(lambda curr_tup: curr_tup[0] == "T", all_unique_captures)

    def get_prep_kit_name(self, prep_kit_code):
        """
        Convert a two-letter library kit code to the corresponding library kit name.

        :param prep_kit_code: Two-letter library prep code. 
        :return: The library prep kit name.
        """

        # FIXME: Move this information to a config JSON file.
        prep_kit_lookup = {"BN": "BIOO_NEXTFLEX",
                           "KH": "KAPA_HYPERPREP",
                           "TD": "THRUPLEX_DNASEQ",
                           "TP": "THRUPLEX_PLASMASEQ",
                           "TF": "THRUPLEX_FD",
                           "TS": "TRUSEQ_RNA",
                           "NN": "NEBNEXT_RNA",
                           "VI": "VILO_RNA"}

        return prep_kit_lookup[prep_kit_code]

    def get_capture_name(self, capture_kit_code):
        """
        Convert a two-letter capture kit code to the corresponding capture kit name.

        :param capture_kit_code: The two-letter capture kit code.
        :return: The capture-kit name.
        """

        # FIXME: Move this information to a config JSON file.
        capture_kit_loopkup = {"CS": "clinseq_v3_targets",
                               "CZ": "clinseq_v4",
                               "EX": "EXOMEV3",
                               "EO": "EXOMEV1",
                               "RF": "fusion_v1",
                               "CC": "core_design",
                               "CD": "discovery_coho",
                               "CB": "big_design",
                               "AL": "alascca_targets",
                               "TT": "test-regions",
                               "CP": "progression",
                               "CM": "monitor"
                               }

        if capture_kit_code == 'WG':
            return 'lowpass_wgs'

        else:
            return capture_kit_loopkup[capture_kit_code]

    def get_all_clinseq_barcodes(self):
        """
        :return: All clinseq barcodes included in this clinseq analysis pipeline's panel data.
        """
        all_panel_clinseq_barcodes = \
            self.sampledata['panel']['T'] + \
            self.sampledata['panel']['N'] + \
            self.sampledata['panel']['CFDNA']
        return filter(lambda bc: bc != None, all_panel_clinseq_barcodes)

    def get_unique_capture_to_clinseq_barcodes(self):
        """
        Retrieves all clinseq barcodes for this clinseq analysis, and organises them according
        to unique library captures.

        :return: A dictionary with tuples indicating unique library captures as keys,
        and barcode lists as values. 
        """
        capture_to_barcodes = collections.defaultdict(list)
        for clinseq_barcode in self.get_all_clinseq_barcodes():
            unique_capture = parse_capture_tuple(clinseq_barcode)
            capture_to_barcodes[unique_capture].append(clinseq_barcode)

        return capture_to_barcodes

    def merge_and_rm_dup(self, unique_capture, input_bams):
        """
        Configures Picard merging and duplicate marking, for the specified group input bams,
        which should all correspond to the specified sample library capture.
        
        Registers the final output bam file for this library capture in this analysis.

        :param sample_type: Clinseq sample type
        :param sample_id: Sample ID
        :param prep_kit_id: Two-letter prep kit ID
        :param capture_kit_id: Two-letter capture kit ID
        :input_bams: The bam filenames for which to do merging and duplicate marking
        """

        # Strings indicating the sample and capture, for use in output file names below:
        sample_str = "{}-{}".format(unique_capture.sample_type, unique_capture.sample_id)
        capture_str = "{}-{}-{}".format(sample_str, unique_capture.prep_kit_id, unique_capture.capture_kit_id)

        # Configure merging:
        merged_bam_filename = \
            "{}/bams/panel/{}.bam".format(self.outdir, capture_str)
        merge_bams = PicardMergeSamFiles(input_bams, merged_bam_filename)
        merge_bams.is_intermediate = True
        merge_bams.jobname = "picard-mergebams-{}".format(sample_str)
        self.add(merge_bams)

        # Configure duplicate marking:
        mark_dups_bam_filename = \
            "{}/bams/panel/{}-nodups.bam".format(self.outdir, capture_str)
        mark_dups_metrics_filename = \
            "{}/qc/picard/panel/{}-markdups-metrics.txt".format(self.outdir, capture_str)
        markdups = PicardMarkDuplicates(\
            merge_bams.output_bam, mark_dups_bam_filename, mark_dups_metrics_filename)
        markdups.is_intermediate = False
        self.add(markdups)

        self.set_capture_bam(unique_capture, markdups.output_bam)

        self.qc_files.append(markdups.output_metrics)

    def get_all_fastq_files(self):
        """
        Get all fastq files that exist for this pipeline instance.

        :return: A list of fastq filenames. 
        """

        fqs = []
        for clinseq_barcode in self.get_all_clinseq_barcodes():
            curr_fqs = find_fastqs(clinseq_barcode, self.libdir)
            fqs.append(curr_fqs)

        return fqs

    def configure_fastq_qcs(self):
        """
        Configure QC on all fastq files that exist for this pipeline instance.
        :return: List of qc output filenames.
        """

        qc_files = []
        for fq in self.get_all_fastq_files():
            basefn = stripsuffix(os.path.basename(fq), ".fastq.gz")
            fastqc = FastQC()
            fastqc.input = fq
            fastqc.outdir = "{}/qc/fastqc/".format(self.outdir)
            fastqc.output = "{}/qc/fastqc/{}_fastqc.zip".format(self.outdir, basefn)
            fastqc.jobname = "fastqc-{}".format(basefn)
            qc_files.append(fastqc.output)
            self.add(fastqc)

        return qc_files

    def configure_align_and_merge(self):
        capture_to_barcodes = self.get_unique_capture_to_clinseq_barcodes()
        for unique_capture in capture_to_barcodes.keys():
            curr_bamfiles = []
            for clinseq_barcode in capture_to_barcodes[unique_capture]:
                curr_bamfiles.append(\
                    align_library(self,
                                  fq1_files=find_fastqs(clinseq_barcode, self.libdir)[0],
                                  fq2_files=find_fastqs(clinseq_barcode, self.libdir)[1],
                                  lib=clinseq_barcode,
                                  ref=self.refdata['bwaIndex'],
                                  outdir=self.outdir + "/bams/panel",
                                  maxcores=self.maxcores))

            self.merge_and_rm_dup(unique_capture, curr_bamfiles)

    def call_germline_variants(self, normal_capture, bam):
        """
        Configure calling of germline variants for a normal sample library capture,
        and configure VEP if specified in the analysis.

        :param normal_capture: The normal sample library capture identifier.
        :param bam: Bam filename input to variant calling.
        """

        targets = self.get_capture_name(normal_capture.capture_kit_id)
        capture_str = "{}-{}-{}".format(normal_capture.sample_id,
                                        normal_capture.prep_kit_id,
                                        normal_capture.capture_kit_id)

        freebayes = Freebayes()
        freebayes.input_bams = [bam]
        freebayes.somatic_only = False
        freebayes.params = None
        freebayes.reference_sequence = self.refdata['reference_genome']
        freebayes.target_bed = self.refdata['targets'][targets]['targets-bed-slopped20']
        freebayes.threads = self.maxcores
        freebayes.scratch = self.scratch
        freebayes.output = "{}/variants/{}.freebayes-germline.vcf.gz".format(self.outdir, capture_str)
        freebayes.jobname = "freebayes-germline-{}".format(capture_str)
        self.add(freebayes)

        if self.refdata['vep_dir']:
            vep_freebayes = VEP()
            vep_freebayes.input_vcf = freebayes.output
            vep_freebayes.threads = self.maxcores
            vep_freebayes.reference_sequence = self.refdata['reference_genome']
            vep_freebayes.vep_dir = self.refdata['vep_dir']
            vep_freebayes.output_vcf = "{}/variants/{}.freebayes-germline.vep.vcf.gz".format(self.outdir, capture_str)
            vep_freebayes.jobname = "vep-freebayes-germline-{}".format(capture_str)
            self.add(vep_freebayes)

            self.set_germline_vcf(normal_capture, vep_freebayes.output_vcf)
        else:
            self.set_germline_vcf(normal_capture, freebayes.output)

    def configure_panel_analysis_with_normal(self, normal_capture):
        """
        Configure panel analyses focused on a specific unique normal library capture.
        """

        if normal_capture[0] is not "N":
            raise ValueError("Invalid input tuple: " + normal_capture)

        normal_bam = self.get_capture_bam(normal_capture)
        # Configure germline variant calling:
        self.call_germline_variants(normal_capture, normal_bam)

        # For each unique cancer library capture, configure a comparative analysis against
        # this normal capture:
        for cancer_capture in self.get_unique_cancer_captures():
            self.configure_panel_analysis_cancer_vs_normal(\
                    normal_capture, cancer_capture)

    def configure_single_capture_analysis(self, unique_capture):
        """
        Configure all general analyses to perform given a single sample library capture.
        """

        input_bam = self.get_capture_bam(unique_capture)
        sample_str = compose_sample_str(unique_capture)
        targets = self.get_capture_name(unique_capture.capture_kit_id)

        # Configure CNV kit analysis:
        cnvkit = CNVkit(input_bam=input_bam,
                        output_cnr="{}/cnv/{}.cnr".format(self.outdir, sample_str),
                        output_cns="{}/cnv/{}.cns".format(self.outdir, sample_str),
                        scratch=self.scratch)

        # If we have a CNVkit reference
        if self.refdata['targets'][targets]['cnvkit-ref']:
            cnvkit.reference = self.refdata['targets'][targets]['cnvkit-ref']
        else:
            cnvkit.targets_bed = self.refdata['targets'][targets]['targets-bed-slopped20']

        cnvkit.jobname = "cnvkit/{}".format(sample_str)

        # Register the result of this analysis:
        self.set_capture_cnr(unique_capture, cnvkit.output_cnr)
        self.set_capture_cns(unique_capture, cnvkit.output_cns)

        self.add(cnvkit)

    def configure_panel_analyses(self):
        """
        Configure generic analyses of all panel data for this clinseq pipeline.

        Populates self.capture_to_merged_bam and normal_capture_to_results. 
        """

        # Configure alignment and merging for each unique sample library capture:
        self.configure_align_and_merge()

        # Configure analyses to be run on all unique panel captures individually:
        for unique_capture in self.get_unique_cancer_captures():
            self.configure_single_capture_analysis(unique_capture)

        # Configure a separate group of analyses for each unique normal library capture:
        for normal_capture in self.get_unique_normal_captures():
            self.configure_panel_analysis_with_normal(normal_capture)

    def configure_somatic_calling(self, normal_capture, cancer_capture):
        # FIXME: Need to fix the configuration of the min_alt_frac threshold, rather than hard-coding it here:
        somatic_variants = call_somatic_variants(
            self, tbam=self.get_capture_bam(normal_capture), nbam=self.get_capture_bam(cancer_capture),
            tlib=compose_sample_str(cancer_capture), nlib=compose_sample_str(normal_capture),
            target_name=self.get_capture_name(cancer_capture.capture_kit_id),
            refdata=self.refdata, outdir=self.outdir,
            callers=['vardict'], vep=self.get_vep(), min_alt_frac=0.02)
        self.normal_cancer_pair_to_results[(normal, cancer_capture)].somatic_vcf = somatic_variants

    def configure_vcf_add_sample(self, normal_capture, cancer_capture):
        # Configure VCF add sample:
        vcfaddsample = VcfAddSample()
        vcfaddsample.input_bam = self.get_capture_bam(cancer_capture)
        vcfaddsample.input_vcf = self.get_germline_vcf(normal_capture)
        normal_sample_str = compose_sample_str(normal_capture)
        cancer_sample_str = compose_sample_str(cancer_capture)
        vcfaddsample.samplename = cancer_sample_str
        vcfaddsample.filter_hom = True
        vcfaddsample.output = "{}/variants/{}-and-{}.germline-variants-with-somatic-afs.vcf.gz".format(
            self.outdir, normal_sample_str, cancer_sample_str)
        vcfaddsample.jobname = "vcf-add-sample-{}".format(cancer_sample_str)
        self.add(vcfaddsample)
        self.normal_cancer_pair_to_results[(normal_capture, cancer_capture)].vcf_addsample_output = \
            vcfaddsample.output

    def configure_msi_sensor(self, normal_capture, cancer_capture):
        # Configure MSI sensor:
        msisensor = MsiSensor()
        msisensor.msi_sites = self.refdata['targets'][cancer_capture.capture_kit_id]['msisites']
        msisensor.input_normal_bam = self.get_capture_bam(normal_capture)
        msisensor.input_tumor_bam = self.get_capture_bam(cancer_capture)
        normal_capture_str = compose_sample_str(normal_capture)
        cancer_capture_str = compose_sample_str(cancer_capture)
        msisensor.output = "{}/msisensor-{}-{}.tsv".format(
            self.outdir, normal_capture_str, cancer_capture_str)
        msisensor.threads = self.maxcores
        msisensor.jobname = "msisensor-{}-{}".format(normal_capture_str, cancer_capture_str)
        self.normal_cancer_pair_to_results[(normal_capture, cancer_capture)].msi_output = \
            msisensor.output
        self.add(msisensor)

    def configure_hz_conc(self, normal_capture, cancer_capture):
        # Configure heterozygote concordance:
        hzconcordance = HeterzygoteConcordance()
        hzconcordance.input_vcf = self.get_germline_vcf(normal_capture)
        hzconcordance.input_bam = self.get_capture_bam(cancer_capture)
        hzconcordance.reference_sequence = self.refdata['reference_genome']
        hzconcordance.target_regions = \
            self.refdata['targets'][cancer_capture.capture_kit_id]['targets-interval_list-slopped20']
        hzconcordance.normalid = compose_sample_str(normal_capture)
        hzconcordance.filter_reads_with_N_cigar = True
        hzconcordance.jobname = "hzconcordance-{}".format(compose_sample_str(cancer_capture))
        hzconcordance.output = "{}/bams/{}-{}-hzconcordance.txt".format(
            self.outdir, compose_sample_str(cancer_capture), compose_sample_str(normal_capture))
        self.normal_cancer_pair_to_results[(normal_capture, cancer_capture)].hzconcordance_output = \
            hzconcordance.output
        self.add(hzconcordance)

    def configure_contest_vcf_generation(self, normal_capture, cancer_capture):
        """Configure generation of a contest VCF for a specified normal, cancer
        library capture pairing."""

        contest_vcf_generation = CreateContestVCFs()
        contest_vcf_generation.input_population_vcf = self.refdata['swegene_common']
        contest_vcf_generation.input_target_regions_bed_1 = normal_capture
        contest_vcf_generation.input_target_regions_bed_2 = cancer_capture
        normal_capture_str = compose_sample_str(normal_capture)
        cancer_capture_str = compose_sample_str(cancer_capture)
        contest_vcf_generation.output = "{}/contamination/pop_vcf_{}-{}.vcf".format(
            normal_capture_str, cancer_capture_str)
        contest_vcf_generation.jobname = "contest_pop_vcf_{}-{}".format(
            normal_capture_str, cancer_capture_str)
        self.add(contest_vcf_generation)
        return contest_vcf_generation.output

    def configure_contest(self, library_capture_1, library_capture_2, contest_vcf):
        # Configure contest for the specified pair of library captures:
        contest = ContEst()
        contest.reference_genome = self.refdata['reference_genome']
        contest.input_eval_bam = self.get_capture_bam(library_capture_1)
        contest.input_genotype_bam = self.get_capture_bam(library_capture_2)
        contest.input_population_af_vcf = contest_vcf
        # TODO: Is it necessary to create the output subdir contamination somewhere? Check how it's done for e.g. cnvkit.
        contest.output = "{}/contamination/{}.contest.txt".format(self.outdir, compose_sample_str(library_capture_1)) # TODO: Should the analysis id also be in name of out file?
        contest.jobname = "contest_tumor/{}".format(compose_sample_str(library_capture_1))  # TODO: Is it ok that the job name does not contain analysis id, i.e. may not be unique?
        self.add(contest)
        return contest.output

    def configure_contam_qc_call(self, contest_output):
        # Generate ContEst contamination QC call JSON files from the ContEst
        # outputs:
        process_contest = ContEstToContamCaveat()
        process_contest.input_contest_results = contest_output
        process_contest.output = "{}/qc/{}-contam-qc-call.json".format(self.outdir, self.sampledata['panel']['T'])
        self.add(process_contest)
        return process_contest.output

    def configure_contamination_estimate(self, normal_capture, cancer_capture):
        # Configure generation of the contest VCF input file:
        intersection_contest_vcf = \
            self.configure_contest_vcf_generation(normal_capture, cancer_capture)

        # Configure contest for calculating contamination in the cancer sample:
        cancer_vs_normal_contest_output = \
            self.configure_contest(cancer_capture, normal_capture, intersection_contest_vcf)

        # Configure contest for calculating contamination in the normal sample:
        normal_vs_cancer_contest_output = \
            self.configure_contest(normal_capture, cancer_capture, intersection_contest_vcf)

        # Configure cancer sample contamination QC call:
        cancer_contam_call = self.configure_contam_qc_call(self, cancer_vs_normal_contest_output)

        # Register the outputs of running contest:
        self.normal_cancer_pair_to_results[(normal_capture, cancer_capture)].normal_contest_output = \
            normal_vs_cancer_contest_output
        self.normal_cancer_pair_to_results[(normal_capture, cancer_capture)].cancer_contest_output = \
            cancer_vs_normal_contest_output
        self.normal_cancer_pair_to_results[(normal_capture, cancer_capture)].cancer_contam_call = \
            cancer_contam_call

    def configure_panel_analysis_cancer_vs_normal(self, normal_capture, cancer_capture):
        """
        Configures standard paired cancer vs normal panel analyses for the specified unique
        normal and cancer library captures.

        Comprises the following analyses:
        - Somatic variant calling
        - Updating of the germline VCF to take into consideration the cancer sample
        - MSI sensor
        - Heterozygote concordance of the sample pair
        - Contamination estimate of cancer compared with normal and vice versa

        :param normal_capture: A unique normal sample library capture
        :param cancer_capture: A unique cancer sample library capture
        """

        self.configure_somatic_calling(normal_capture, cancer_capture)
        self.configure_vcf_add_sample(normal_capture, cancer_capture)
        self.configure_msi_sensor(normal_capture, cancer_capture)
        self.configure_hz_conc(normal_capture, cancer_capture)
        self.configure_contamination_estimate(normal_capture, cancer_capture)

    def configure_all_panel_qcs(self):
        for unique_capture in self.get_all_unique_captures():
            self.qc_files += \
                self.configure_panel_qc(unique_capture)

    def configure_multi_qc(self):
        multiqc = MultiQC()
        multiqc.input_files = self.qc_files
        multiqc.search_dir = self.outdir
        multiqc.output = "{}/multiqc/{}-multiqc".format(self.outdir, self.sampledata['sdid'])
        multiqc.jobname = "multiqc-{}".format(self.sampledata['sdid'])
        self.add(multiqc)

    def configure_panel_qc(self, unique_capture):
        """
        Configure QC analyses for a given library capture.
        :param bams: list of bams
        :return: list of generated files
        """

        bam = self.get_capture_bam(unique_capture)

        targets = self.get_capture_name(unique_capture.capture_kit_id)
        logging.debug("Adding QC jobs for {}".format(bam))

        capture_str = compose_sample_str(unique_capture)

        isize = PicardCollectInsertSizeMetrics()
        isize.input = bam
        isize.output_metrics = "{}/qc/picard/panel/{}.picard-insertsize.txt".format(self.outdir, capture_str)
        isize.jobname = "picard-isize-{}".format(capture_str)
        self.add(isize)

        oxog = PicardCollectOxoGMetrics()
        oxog.input = bam
        oxog.reference_sequence = self.refdata['reference_genome']
        oxog.output_metrics = "{}/qc/picard/panel/{}.picard-oxog.txt".format(self.outdir, capture_str)
        oxog.jobname = "picard-oxog-{}".format(capture_str)
        self.add(oxog)

        hsmetrics = PicardCollectHsMetrics()
        hsmetrics.input = bam
        hsmetrics.reference_sequence = self.refdata['reference_genome']
        hsmetrics.target_regions = self.refdata['targets'][targets][
            'targets-interval_list-slopped20']
        hsmetrics.bait_regions = self.refdata['targets'][targets][
            'targets-interval_list-slopped20']
        hsmetrics.bait_name = targets
        hsmetrics.output_metrics = "{}/qc/picard/panel/{}.picard-hsmetrics.txt".format(self.outdir, capture_str)
        hsmetrics.jobname = "picard-hsmetrics-{}".format(capture_str)
        self.add(hsmetrics)

        sambamba = SambambaDepth()
        sambamba.targets_bed = self.refdata['targets'][targets]['targets-bed-slopped20']
        sambamba.input = bam
        sambamba.output = "{}/qc/sambamba/{}.sambamba-depth-targets.txt".format(self.outdir, capture_str)
        sambamba.jobname = "sambamba-depth-{}".format(capture_str)
        self.add(sambamba)

        coverage_hist = CoverageHistogram()
#        if 'alascca_targets' in self.refdata['targets']:
#            alascca_coverage_hist.input_bed = self.refdata['targets']['alascca_targets']['targets-bed-slopped20']
#        else:
        coverage_hist.input_bed = self.refdata['targets'][targets]['targets-bed-slopped20']
        coverage_hist.input_bam = bam
        coverage_hist.output = "{}/qc/{}.coverage-histogram.txt".format(self.outdir, capture_str)
        coverage_hist.jobname = "alascca-coverage-hist/{}".format(capture_str)
        self.add(coverage_hist)

        coverage_qc_call = CoverageCaveat()
        coverage_qc_call.input_histogram = coverage_hist.output
        coverage_qc_call.output = "{}/qc/{}.coverage-qc-call.json".format(self.outdir, capture_str)
        coverage_qc_call.jobname = "coverage-qc-call/{}".format(capture_str)
        self.add(coverage_qc_call)
        self.capture_to_results[unique_capture].cov_qc_call = coverage_qc_call.output

        return [isize.output_metrics, oxog.output_metrics, hsmetrics.output_metrics,
                sambamba.output, coverage_hist.output, coverage_qc_call.output]
