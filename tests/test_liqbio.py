import os
import tempfile
import unittest

import sys

import time

from genomicassertions.readassertions import ReadAssertions
from genomicassertions.variantassertions import VariantAssertions

from autoseq.cli.cli import load_ref, get_runner
from autoseq.pipeline.liqbio import LiqBioPipeline
from autoseq.util.path import normpath


class TestWorkflow(unittest.TestCase, VariantAssertions, ReadAssertions):
    returncode = None
    tmpdir = None
    outdir = None
    somatic_vcf = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = normpath("~/tmp/")  # tempfile.mkdtemp()
        cls.outdir = normpath("~/tmp/autoseq-test")  # tempfile.mkdtemp()

        ref = load_ref(normpath("~/test-genome/autoseq-genome.json"))
        sampledata = {
            "sdid": "NA12877",
            "panel": {
                "T": "NA12877-T-03098849-TD1-TT1",
                "N": "NA12877-N-03098121-TD1-TT1",
                "P": ["NA12877-P-03098850-TD1-TT1", "NA12877-P-03098850-TD2-TT1"]
            },
            "wgs": {
                "T": "NA12877-T-03098849-TD1-WGS",
                "N": "NA12877-N-03098121-TD1-WGS",
                "P": ["NA12877-P-03098850-TD1-WGS"]
            }
        }

        libdir = normpath("~/libraries")

        maxcores = 1
        runner = get_runner("shellrunner", maxcores)

        jobdb = os.path.join(cls.outdir, "liqbio.json")
        p = LiqBioPipeline(sampledata, ref, cls.outdir, libdir, analysis_id="test",
                           maxcores=maxcores, runner=runner, jobdb=jobdb)

        p.start()

        try:
            while p.is_alive():
                time.sleep(1)
        except Exception, e:
            print e.message
            sys.exit(1)

        if p.exitcode != 0:
            print "exitcode was {}".format(p.exitcode)
            raise OSError

    def test_vardict_somatic(self):
        vcf = os.path.join(self.outdir, "variants",
                           "NA12877-T-03098849-NA12877-N-03098121-TD1-TT1.vardict-somatic.vcf.gz")

        # TP53 insertion: MU2185182, chr17:g.7578475->G
        # TP53 deletion: MU25947, chr17:g.7577558G>-
        # TP53 DNV: MU52971976, chr17:g.7574003GG>AA
        # PIK3CA hotspot E545K, MU5219, chr3:g.178936091G>A
        # PTEN hotspot R130Q, MU29098, chr10:g.89692905G>A
        # PTEN hotspot R233*, MU589331, chr10:g.89717672C>T
        # AR intron variant, MU50988553, chrX:g.66788924G>A

        self.assertVcfHasSample(vcf, 'NA12877-N-03098121-TD1-TT1')  # N lib id is set
        self.assertVcfHasSample(vcf, 'NA12877-T-03098849')  # T lib id is the merged library

        # deletion is called
        self.assertVcfHasVariantWithChromPosRefAlt(vcf, 17, 7577557, 'AG', 'A')

        # insertion is called
        self.assertVcfHasVariantWithChromPosRefAlt(vcf, 17, 7578474, 'C', 'CG')

        # dnv (GG>AA) is called as a single event
        self.assertVcfHasVariantWithChromPosRefAlt(vcf, 17, 7574003, 'GG', 'AA')

        # PTEN hotspots are called
        self.assertVcfHasVariantWithChromPosRefAlt(vcf, 10, 89692905, 'G', 'A')
        self.assertVcfHasVariantWithChromPosRefAlt(vcf, 10, 89717672, 'C', 'T')

        # PIK3CA hotspot is called
        self.assertVcfHasVariantWithChromPosRefAlt(vcf, 3, 178936091, 'G', 'A')

    def test_qdnaseq(self):
        qdnaseq_file_names = ["NA12877-T-03098849-TD1-WGS-qdnaseq.segments.txt",
                              "NA12877-P-03098850-TD1-WGS-qdnaseq.segments.txt",
                              "NA12877-N-03098121-TD1-WGS-qdnaseq.segments.txt"]
        for qdnaseqf in qdnaseq_file_names:
            absf = os.path.join(self.outdir, "cnv", qdnaseqf)

            with open(absf) as segments:
                ln = segments.readline().strip()
                header = ln.split("\t")
                correct_header = ["chromosome", "start", "end", "bases", "gc", "mappability", "blacklist",
                                  "residual", "use", "readcount", "copynumber", "segmented"]
                self.assertListEqual(header, correct_header)

    def test_wgs_bam_coverage(self):
        bam = os.path.join(self.outdir, "bams/wgs/",
                           "NA12877-T-03098849-TD1-WGS.bam")
        self.assertBamHasCoverageAt(bam, 1, '3', 3617655)  # 1x coverage in that position
        self.assertBamHasCoverageAt(bam, 0, '3', 3618655)  # no coverage in that position

    def test_germline_vcf(self):
        vcf = os.path.join(self.outdir, "variants",
                           "NA12877-N-03098121-TD1-TT1.freebayes-germline.vcf.gz")

        self.assertVcfHasSample(vcf, 'NA12877-N-03098121-TD1-TT1')  # sample is the name of the normal lib id
        self.assertVcfHasVariantWithChromPosRefAlt(vcf, '3', 178925677, 'G', 'A')  # SNP
        self.assertVcfHasVariantWithChromPosRefAlt(vcf, '17', 7579643, 'CCCCCAGCCCTCCAGGT', 'C')  # deletion

