import argparse

import fim
import pandas as pd
import scipy.stats as stats

import export
import outlier
import preprocess


##############################################################################

# DATA LOADING AND PRE-PROCESSING

def load_metrics(file_in, min_var, min_corr, scaling):
    # load data from the input file
    data_raw = preprocess.load_metrics(file_in)

    # pre-process: remove low-variance and correlated metrics & scale the values
    data, variance, corr = preprocess.preprocess(data_raw, min_variance=min_var, min_corr=min_corr, scaling_mode=scaling)

    # add the preprocessing results to the qcML export
    exporter.low_variance(pd.Series(variance, index=data_raw.columns.values), min_var)
    exporter.correlation(corr, min_corr)

    # add general visualizations to the qcML export
    exporter.global_visualization(data)

    return data


##############################################################################

# OUTLIER DETECTION

def detect_outliers(data, k, dist, outlier_threshold=None, num_bins=20):
    # compute outlier scores
    outlier_scores = outlier.detect_outliers_loop(data, k, metric=dist)

    # compute the outlier threshold (if required)
    if outlier_threshold is None:
        outlier_threshold = outlier.detect_outlier_score_threshold(outlier_scores, num_bins)

    # add the outlier score information to the qcML export
    exporter.outlier_scores(outlier_scores, outlier_threshold, num_bins)

    # remove significant outliers
    data_including_outliers = data
    data, outliers = outlier.split_outliers(data, outlier_scores, outlier_threshold)

    # retrieve explanatory subspaces for each outlier
    outliers['FeatureImportance'] = object
    outliers['Subspace'] = object
    for name, this_outlier in outliers.iterrows():
        feature_importance, subspace = outlier.get_outlier_subspace(data_including_outliers, this_outlier, k)
        outliers.set_value(name, 'FeatureImportance', feature_importance.values)
        outliers.set_value(name, 'Subspace', subspace)

        # add the outlier to the qcML export
        exporter.outlier(outliers.loc[name], data)

    return data, outliers


def analyze_outliers(outliers, min_sup, min_length):
    # detect frequently occurring explanatory subspaces
    frequent_subspaces = sorted(fim.fim(outliers.Subspace, supp=min_sup, zmin=min_length), key=lambda x: x[1][0], reverse=True)
    frequent_subspaces_table = pd.DataFrame(index=range(len(frequent_subspaces)),
                                            columns=['Outlier subspace QC metric(s)', 'Number of outlying experiments'])
    for i, (subspace, (support,)) in enumerate(frequent_subspaces):
        frequent_subspaces_table.set_value(i, 'Outlier subspace QC metric(s)', ', '.join(subspace))
        frequent_subspaces_table.set_value(i, 'Number of outlying experiments', support)

    exporter.frequent_outlier_subspaces(frequent_subspaces_table, min_sup, min_length)

    return frequent_subspaces


##############################################################################

# OUTLIER VALIDATION BY PSM COMPARISON (for the manuscript)

def compare_outlier_psms(f_psms, outliers):
    # compare inliers and outliers based on their number of valid PSM's
    psms = pd.Series.from_csv(f_psms)

    outlier_psms = psms.filter(items=[index[0] for index in outliers.index.values])
    inlier_psms = psms.drop(outlier_psms.index)

    exporter.psm(inlier_psms, outlier_psms)

    return psms, inlier_psms, outlier_psms


def compare_outlier_subspace_psms(outliers, frequent_subspaces, psms, inlier_psms):
    # test whether a subspace can be related to a lower number of PSM's
    psm_table = pd.DataFrame(index=psms.index)
    psm_table['\\bfseries Inliers'] = inlier_psms
    pval_table = pd.DataFrame(index=range(len(frequent_subspaces)), columns=['Metric(s)', 'Number of outliers', '\emph{p}-value'])
    for i, (subspace, (support,)) in enumerate(frequent_subspaces):
        subspace = map(None, subspace)

        # compare outlier values
        outliers_values = pd.DataFrame([this_outlier for _, this_outlier in outliers.iterrows() if set(subspace) <= set(this_outlier.Subspace)])

        # compare outlier PSM's
        outlier_psms = psms.filter(items=[index[0] for index in outliers_values.index.values])

        # quantify difference between inliers and outliers
        t_stat, p_value = stats.ttest_ind(inlier_psms.values, outlier_psms.values, equal_var=False)

        psm_table['{}{}'.format('\\itshape ' if p_value <= 0.05 else '', ', '.join(subspace))] = outlier_psms

        pval_table.set_value(i, 'Metric(s)', ', '.join(subspace))
        pval_table.set_value(i, 'Number of outliers', support)
        pval_table.set_value(i, '\emph{p}-value', p_value)

    exporter.psm_pval(psm_table, pval_table)


##############################################################################

#  EXECUTE

def parse_args():
    parser = argparse.ArgumentParser(description='Mass spectrometry quality control metrics analysis')
    parser.add_argument('file_in', type=argparse.FileType('r'),
                        help='the tab-separated input file containing the QC metrics')
    parser.add_argument('file_out', type=argparse.FileType('w'),
                        help='the name of the qcML output file')
    parser.add_argument('--min_var', '-var', default=0.0001, type=float,
                        help='metrics with a lower variance will be removed (default: %(default)s)')
    parser.add_argument('--min_corr', '-corr', default=0.9, type=float,
                        help='metrics with a higher correlation will be removed (default: %(default)s)')
    parser.add_argument('--scaling_mode', '-scale', default='robust', type=str, choices=['robust', 'standard'],
                        help='mode to standardize the metric values (default: %(default)s)')
    parser.add_argument('--k_neighbors', '-k', type=int, required=True,
                        help='the number of nearest neighbors used for outlier detection')
    parser.add_argument('--distance', '-dist', default='manhattan', type=str,
                        help='metric to use for distance computation (default: %(default)s) '
                             'ny metric from scikit-learn or scipy.spatial.distance can be used')
    parser.add_argument('--min_outlier', '-o', default=None, type=float,
                        help='the minimum outlier score threshold (default: %(default)s) '
                             'if no threshold is provided, an automatic threshold is determined')
    parser.add_argument('--num_bins', '-bin', default=20, type=int,
                        help='the number of bins for the outlier score histogram (default: %(default)s)')
    parser.add_argument('--min_sup', '-sup', default=5, type=int,
                        help='the minimum support for subspace frequent itemset mining (default: %(default)s) '
                             'positive numbers are interpreted as percentages, negative numbers as absolute supports')
    parser.add_argument('--min_length', '-len', default=1, type=int,
                        help='the minimum length each subspace itemset should be (default: %(default)s)')

    # parse command-line arguments
    return parser.parse_args()


def run(args):
    global exporter
    exporter = export.Exporter(True, False)

    data = load_metrics(args.file_in, args.min_var, args.min_corr, args.scaling_mode)
    data_excluding_outliers, outliers = detect_outliers(data, args.k_neighbors, args.distance, args.min_outlier, args.num_bins)
    frequent_subspaces = analyze_outliers(outliers, args.min_sup, args.min_length)

    exporter.export(args.file_out)


if __name__ == '__main__':
    run(parse_args())

##############################################################################