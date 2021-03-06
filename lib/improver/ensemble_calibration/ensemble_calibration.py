# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# (C) British Crown Copyright 2017-2019 Met Office.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""
This module defines all the "plugins" specific for ensemble calibration.

"""
import numpy as np
from scipy import stats
from scipy.optimize import minimize
from scipy.stats import norm
import warnings

import cf_units as unit
import iris
from iris.exceptions import CoordinateNotFoundError

from improver.ensemble_calibration.ensemble_calibration_utilities import (
    convert_cube_data_to_2d, check_predictor_of_mean_flag)
from improver.utilities.cube_manipulation import (
    concatenate_cubes, enforce_coordinate_ordering)
from improver.utilities.temporal import iris_time_to_datetime


class ContinuousRankedProbabilityScoreMinimisers(object):
    """
    Minimise the Continuous Ranked Probability Score (CRPS)

    Calculate the optimised coefficients for minimising the CRPS based on
    assuming a particular probability distribution for the phenomenon being
    minimised.

    The number of coefficients that will be optimised depend upon the initial
    guess.

    Minimisation is performed using the Nelder-Mead algorithm for 200
    iterations to limit the computational expense.
    Note that the BFGS algorithm was initially trialled but had a bug
    in comparison to comparative results generated in R.

    """

    # Maximum iterations for minimisation using Nelder-Mead.
    MAX_ITERATIONS = 200

    # The tolerated percentage change for the final iteration when
    # performing the minimisation.
    TOLERATED_PERCENTAGE_CHANGE = 5

    # An arbitrary value set if an infinite value is detected
    # as part of the minimisation.
    BAD_VALUE = np.float64(999999)

    def __init__(self):
        # Dictionary containing the minimisation functions, which will
        # be used, depending upon the distribution, which is requested.
        self.minimisation_dict = {
            "gaussian": self.normal_crps_minimiser,
            "truncated gaussian": self.truncated_normal_crps_minimiser}

    def crps_minimiser_wrapper(
            self, initial_guess, forecast_predictor, truth, forecast_var,
            predictor_of_mean_flag, distribution):
        """
        Function to pass a given minimisation function to the scipy minimize
        function to estimate optimised values for the coefficients.

        Args:
            initial_guess (List):
                List of optimised coefficients.
                Order of coefficients is [gamma, delta, alpha, beta].
            forecast_predictor (iris.cube.Cube):
                Cube containing the fields to be used as the predictor,
                either the ensemble mean or the ensemble realizations.
            truth (iris.cube.Cube):
                Cube containing the field, which will be used as truth.
            forecast_var (iris.cube.Cube):
                Cube containg the field containing the ensemble variance.
            predictor_of_mean_flag (String):
                String to specify the input to calculate the calibrated mean.
                Currently the ensemble mean ("mean") and the ensemble
                realizations ("realizations") are supported as the predictors.
            distribution (String):
                String used to access the appropriate minimisation function
                within self.minimisation_dict.

        Returns:
            optimised_coeffs (List):
                List of optimised coefficients.
                Order of coefficients is [gamma, delta, alpha, beta].

        """
        def calculate_percentage_change_in_last_iteration(allvecs):
            """
            Calculate the percentage change that has occurred within
            the last iteration of the minimisation. If the percentage change
            between the last iteration and the last-but-one iteration exceeds
            the threshold, a warning message is printed.

            Args:
                allvecs : List
                    List of numpy arrays containing the optimised coefficients,
                    after each iteration.
            """
            last_iteration_percentage_change = np.absolute(
                (allvecs[-1] - allvecs[-2]) / allvecs[-2])*100
            if (np.any(last_iteration_percentage_change >
                       self.TOLERATED_PERCENTAGE_CHANGE)):
                np.set_printoptions(suppress=True)
                msg = ("\nThe final iteration resulted in a percentage change "
                       "that is greater than the accepted threshold of 5% "
                       "i.e. {}. "
                       "\nA satisfactory minimisation has not been achieved. "
                       "\nLast iteration: {}, "
                       "\nLast-but-one iteration: {}"
                       "\nAbsolute difference: {}\n").format(
                           last_iteration_percentage_change, allvecs[-1],
                           allvecs[-2], np.absolute(allvecs[-2]-allvecs[-1]))
                warnings.warn(msg)

        try:
            minimisation_function = self.minimisation_dict[distribution]
        except KeyError as err:
            msg = ("Distribution requested {} is not supported in {}"
                   "Error message is {}".format(
                       distribution, self.minimisation_dict, err))
            raise KeyError(msg)

        # Ensure predictor_of_mean_flag is valid.
        check_predictor_of_mean_flag(predictor_of_mean_flag)

        if predictor_of_mean_flag.lower() in ["mean"]:
            forecast_predictor_data = forecast_predictor.data.flatten()
            truth_data = truth.data.flatten()
            forecast_var_data = forecast_var.data.flatten()
        elif predictor_of_mean_flag.lower() in ["realizations"]:
            truth_data = truth.data.flatten()
            forecast_predictor = (
                enforce_coordinate_ordering(
                    forecast_predictor, "realization"))
            forecast_predictor_data = convert_cube_data_to_2d(
                forecast_predictor)
            forecast_var_data = forecast_var.data.flatten()

        initial_guess = np.array(initial_guess, dtype=np.float32)
        forecast_predictor_data = forecast_predictor_data.astype(np.float32)
        forecast_var_data = forecast_var_data.astype(np.float32)
        truth_data = truth_data.astype(np.float32)
        sqrt_pi = np.sqrt(np.pi).astype(np.float32)

        optimised_coeffs = minimize(
            minimisation_function, initial_guess,
            args=(forecast_predictor_data, truth_data,
                  forecast_var_data, sqrt_pi, predictor_of_mean_flag),
            method="Nelder-Mead",
            options={"maxiter": self.MAX_ITERATIONS, "return_all": True})
        if not optimised_coeffs.success:
            msg = ("Minimisation did not result in convergence after "
                   "{} iterations. \n{}".format(
                       self.MAX_ITERATIONS, optimised_coeffs.message))
            warnings.warn(msg)
        calculate_percentage_change_in_last_iteration(optimised_coeffs.allvecs)
        return optimised_coeffs.x

    def normal_crps_minimiser(
            self, initial_guess, forecast_predictor, truth, forecast_var,
            sqrt_pi, predictor_of_mean_flag):
        """
        Minimisation function to calculate coefficients based on minimising the
        CRPS for a normal distribution.

        Scientific Reference:
        Gneiting, T. et al., 2005.
        Calibrated Probabilistic Forecasting Using Ensemble Model Output
        Statistics and Minimum CRPS Estimation.
        Monthly Weather Review, 133(5), pp.1098-1118.

        Args:
            initial_guess : List
                List of optimised coefficients.
                Order of coefficients is [gamma, delta, alpha, beta].
            forecast_predictor : Numpy array
                Data to be used as the predictor,
                either the ensemble mean or the ensemble realizations.
            truth : Numpy array
                Data to be used as truth.
            forecast_var : Numpy array
                Ensemble variance data.
            sqrt_pi : Numpy array
                Square root of Pi
            predictor_of_mean_flag : String
                String to specify the input to calculate the calibrated mean.
                Currently the ensemble mean ("mean") and the ensemble
                realizations ("realizations") are supported as the predictors.

        Returns:
            result (Float):
                Minimum value for the CRPS achieved.

        """
        if predictor_of_mean_flag.lower() in ["mean"]:
            beta = initial_guess[2:]
        elif predictor_of_mean_flag.lower() in ["realizations"]:
            beta = np.array(
                [initial_guess[2]]+(initial_guess[3:]**2).tolist(),
                dtype=np.float32
            )

        new_col = np.ones(truth.shape, dtype=np.float32)
        all_data = np.column_stack((new_col, forecast_predictor))
        mu = np.dot(all_data, beta)
        sigma = np.sqrt(
            initial_guess[0]**2 + initial_guess[1]**2 * forecast_var)
        xz = (truth - mu) / sigma
        normal_cdf = norm.cdf(xz)
        normal_pdf = norm.pdf(xz)
        result = np.nansum(
            sigma * (xz * (2 * normal_cdf - 1) + 2 * normal_pdf - 1 / sqrt_pi))
        if not np.isfinite(np.min(mu/sigma)):
            result = self.BAD_VALUE
        return result

    def truncated_normal_crps_minimiser(
            self, initial_guess, forecast_predictor, truth, forecast_var,
            sqrt_pi, predictor_of_mean_flag):
        """
        Minimisation function to calculate coefficients based on minimising the
        CRPS for a truncated_normal distribution.

        Scientific Reference:
        Thorarinsdottir, T.L. & Gneiting, T., 2010.
        Probabilistic forecasts of wind speed: Ensemble model
        output statistics by using heteroscedastic censored regression.
        Journal of the Royal Statistical Society.
        Series A: Statistics in Society, 173(2), pp.371-388.

        Args:
            initial_guess (List):
                List of optimised coefficients.
                Order of coefficients is [gamma, delta, alpha, beta].
            forecast_predictor (Numpy array):
                Data to be used as the predictor,
                either the ensemble mean or the ensemble realizations.
            truth (Numpy array):
                Data to be used as truth.
            forecast_var (Numpy array):
                Ensemble variance data.
            sqrt_pi (Numpy array):
                Square root of Pi
            predictor_of_mean_flag (String):
                String to specify the input to calculate the calibrated mean.
                Currently the ensemble mean ("mean") and the ensemble
                realizations ("realizations") are supported as the predictors.

        Returns:
            result (Float):
                Minimum value for the CRPS achieved.

        """
        if predictor_of_mean_flag.lower() in ["mean"]:
            beta = initial_guess[2:]
        elif predictor_of_mean_flag.lower() in ["realizations"]:
            beta = np.array(
                [initial_guess[2]]+(initial_guess[3:]**2).tolist(),
                dtype=np.float32
            )

        new_col = np.ones(truth.shape, dtype=np.float32)
        all_data = np.column_stack((new_col, forecast_predictor))
        mu = np.dot(all_data, beta)
        sigma = np.sqrt(
            initial_guess[0]**2 + initial_guess[1]**2 * forecast_var)
        xz = (truth - mu) / sigma
        normal_cdf = norm.cdf(xz)
        normal_pdf = norm.pdf(xz)
        x0 = mu / sigma
        normal_cdf_0 = norm.cdf(x0)
        normal_cdf_root_two = norm.cdf(np.sqrt(2) * x0)
        result = np.nansum(
            (sigma / normal_cdf_0**2) *
            (xz * normal_cdf_0 * (2 * normal_cdf + normal_cdf_0 - 2) +
             2 * normal_pdf * normal_cdf_0 -
             normal_cdf_root_two / sqrt_pi))
        if not np.isfinite(np.min(mu/sigma)) or (np.min(mu/sigma) < -3):
            result = self.BAD_VALUE
        return result


class EstimateCoefficientsForEnsembleCalibration(object):
    """
    Class focussing on estimating the optimised coefficients for ensemble
    calibration.
    """
    # Logical flag for whether initial guess estimates for the coefficients
    # will be estimated using linear regression i.e.
    # ESTIMATE_COEFFICIENTS_FROM_LINEAR_MODEL_FLAG = True, or whether default
    # values will be used instead i.e.
    # ESTIMATE_COEFFICIENTS_FROM_LINEAR_MODEL_FLAG = False.
    ESTIMATE_COEFFICIENTS_FROM_LINEAR_MODEL_FLAG = True

    def __init__(self, distribution, desired_units,
                 predictor_of_mean_flag="mean"):
        """
        Create an ensemble calibration plugin that, for Nonhomogeneous Gaussian
        Regression, calculates coefficients based on historical forecasts and
        applies the coefficients to the current forecast.

        Args:
            distribution (str):
                Name of distribution. Assume that the current forecast can be
                represented using this distribution.
            desired_units (str or cf_units.Unit):
                The unit that you would like the calibration to be undertaken
                in. The current forecast, historical forecast and truth will be
                converted as required.
            predictor_of_mean_flag (str):
                String to specify the input to calculate the calibrated mean.
                Currently the ensemble mean ("mean") and the ensemble
                realizations ("realizations") are supported as the predictors.

        """
        self.distribution = distribution
        self.desired_units = desired_units
        self.predictor_of_mean_flag = predictor_of_mean_flag
        self.minimiser = ContinuousRankedProbabilityScoreMinimisers()
        # Setting default values for coeff_names. Beta is the final
        # coefficient name in the list, as there can potentially be
        # multiple beta coefficients if the ensemble realizations, rather
        # than the ensemble mean, are provided as the predictor.
        self.coeff_names = ["gamma", "delta", "alpha", "beta"]

        import imp
        try:
            statsmodels_found = imp.find_module('statsmodels')
            statsmodels_found = True
            import statsmodels.api as sm
            self.sm = sm
        except ImportError:
            statsmodels_found = False
            if predictor_of_mean_flag.lower() in ["realizations"]:
                msg = (
                    "The statsmodels can not be imported. "
                    "Will not be able to calculate an initial guess from "
                    "the individual ensemble realizations. "
                    "A default initial guess will be used without "
                    "estimating coefficients from a linear model.")
                warnings.warn(msg, ImportWarning)
        self.statsmodels_found = statsmodels_found

    def __str__(self):
        result = ('<EstimateCoefficientsForEnsembleCalibration: '
                  'distribution: {};' +
                  'desired_units: {}>' +
                  'predictor_of_mean_flag: {}>' +
                  'minimiser: {}')
        return result.format(
            self.distribution, self.desired_units,
            self.predictor_of_mean_flag, self.minimiser)

    def create_coefficients_cube(
            self, optimised_coeffs, current_forecast):
        """Create a cube for storing the coefficients computed using EMOS.

        .. See the documentation for examples of these cubes.
        .. include:: extended_documentation/ensemble_calibration/
           ensemble_calibration/create_coefficients_cube.rst

        Args:
            optimised_coeffs (list):
                List of optimised coefficients.
                Order of coefficients is [gamma, delta, alpha, beta].
            current_forecast (iris.cube.Cube):
                The cube containing the current forecast.

        Returns:
            cube (iris.cube.Cube):
                Cube constructed using the coefficients provided and using
                metadata from the current_forecast cube. The cube contains
                a coefficient_index dimension coordinate where the points
                of the coordinate are integer values and a
                coefficient_name auxiliary coordinate where the points of
                the coordinate are e.g. gamma, delta, alpha, beta.

        """
        if self.predictor_of_mean_flag.lower() == "realizations":
            realization_coeffs = []
            for realization in current_forecast.coord("realization").points:
                realization_coeffs.append(
                    "{}{}".format(self.coeff_names[-1], np.int32(realization)))
            coeff_names = self.coeff_names[:-1] + realization_coeffs
        else:
            coeff_names = self.coeff_names

        if len(optimised_coeffs) != len(coeff_names):
            msg = ("The number of coefficients in {} must equal the "
                   "number of coefficient names {}.".format(
                        optimised_coeffs, coeff_names))
            raise ValueError(msg)

        coefficient_index = iris.coords.DimCoord(
            np.arange(len(optimised_coeffs)),
            long_name="coefficient_index", units="1")
        coefficient_name = iris.coords.AuxCoord(
            coeff_names, long_name="coefficient_name", units="no_unit")
        dim_coords_and_dims = [(coefficient_index, 0)]
        aux_coords_and_dims = [(coefficient_name, 0)]
        for coord_name in (
                ["time", "forecast_period", "forecast_reference_time"]):
            try:
                aux_coords_and_dims.append(
                    (current_forecast.coord(coord_name), None))
            except CoordinateNotFoundError:
                pass
        attributes = {"diagnostic_standard_name": current_forecast.name()}
        for attribute in current_forecast.attributes.keys():
            if attribute.endswith("model_configuration"):
                attributes[attribute] = (
                    current_forecast.attributes[attribute])
        cube = iris.cube.Cube(
            optimised_coeffs, long_name="emos_coefficients", units="1",
            dim_coords_and_dims=dim_coords_and_dims,
            aux_coords_and_dims=aux_coords_and_dims, attributes=attributes)
        return cube

    def compute_initial_guess(
            self, truth, forecast_predictor, predictor_of_mean_flag,
            estimate_coefficients_from_linear_model_flag,
            no_of_realizations=None):
        """
        Function to compute initial guess of the a and beta components of the
        EMOS coefficients by linear regression of the forecast predictor
        and the truth, if requested. Otherwise, default values for a and b
        will be used.

        Default values have been chosen based on Figure 8 in the
        2017 ensemble calibration report available on the Science Plugin
        Documents Confluence page.

        Args:
            truth (iris.cube.Cube):
                Cube containing the field, which will be used as truth.
            forecast_predictor (iris.cube.Cube):
                Cube containing the fields to be used as the predictor,
                either the ensemble mean or the ensemble realizations.
            predictor_of_mean_flag (str):
                String to specify the input to calculate the calibrated mean.
                Currently the ensemble mean ("mean") and the ensemble
                realizations ("realizations") are supported as the predictors.
            estimate_coefficients_from_linear_model_flag (bool):
                Flag whether coefficients should be estimated from
                the linear regression, or static estimates should be used.
            no_of_realizations (int):
                Number of realizations, if ensemble realizations are to be
                used as predictors. Default is None.

        Returns:
            initial_guess (list):
                List of coefficients to be used as initial guess.
                Order of coefficients is [gamma, delta, alpha, beta].

        """

        if (predictor_of_mean_flag.lower() in ["mean"] and
                not estimate_coefficients_from_linear_model_flag):
            initial_guess = [1, 1, 0, 1]
        elif (predictor_of_mean_flag.lower() in ["realizations"] and
              not estimate_coefficients_from_linear_model_flag):
            initial_guess = [1, 1, 0] + np.repeat(
                1, no_of_realizations).tolist()
        elif estimate_coefficients_from_linear_model_flag:
            if predictor_of_mean_flag.lower() in ["mean"]:
                # Find all values that are not NaN.
                truth_not_nan = ~np.isnan(truth.data.flatten())
                forecast_not_nan = ~np.isnan(forecast_predictor.data.flatten())
                combined_not_nan = (
                    np.all(
                        np.row_stack([truth_not_nan, forecast_not_nan]),
                        axis=0))
                if not any(combined_not_nan):
                    gradient, intercept = ([np.nan, np.nan])
                else:
                    gradient, intercept, _, _, _ = (
                        stats.linregress(
                            forecast_predictor.data.flatten()[
                                combined_not_nan],
                            truth.data.flatten()[combined_not_nan]))
                initial_guess = [1, 1, intercept, gradient]
            elif predictor_of_mean_flag.lower() in ["realizations"]:
                if self.statsmodels_found:
                    truth_data = truth.data.flatten()
                    forecast_predictor = (
                        enforce_coordinate_ordering(
                            forecast_predictor, "realization"))
                    forecast_data = np.array(
                        convert_cube_data_to_2d(
                            forecast_predictor, transpose=False),
                        dtype=np.float32
                    )
                    # Find all values that are not NaN.
                    truth_not_nan = ~np.isnan(truth_data)
                    forecast_not_nan = ~np.isnan(forecast_data)
                    combined_not_nan = (
                        np.all(
                            np.row_stack([truth_not_nan, forecast_not_nan]),
                            axis=0))
                    val = self.sm.add_constant(
                        forecast_data[:, combined_not_nan].T)
                    est = self.sm.OLS(truth_data[combined_not_nan], val).fit()
                    intercept = est.params[0]
                    gradient = est.params[1:]
                    initial_guess = [1, 1, intercept]+gradient.tolist()
                else:
                    initial_guess = (
                        [1, 1, 0] + np.repeat(1, no_of_realizations).tolist())
        return np.array(initial_guess, dtype=np.float32)

    def estimate_coefficients_for_ngr(
            self, current_forecast, historic_forecast, truth):
        """
        Using Nonhomogeneous Gaussian Regression/Ensemble Model Output
        Statistics, estimate the required coefficients from historical
        forecasts.

        The main contents of this method is:

        1. Metadata checks to ensure that the current forecast, historic
           forecast and truth exist in a form that can be processed.
        2. Loop through times within the concatenated current forecast cube:

           1. Extract the desired forecast period from the historic forecasts
              to match the current forecasts. Apply unit conversion to ensure
              that historic forecasts have the desired units for calibration.
           2. Extract the relevant truth to co-incide with the time within
              the historic forecasts. Apply unit conversion to ensure
              that the truth has the desired units for calibration.
           3. Calculate mean and variance.
           4. Calculate initial guess at coefficient values by performing a
              linear regression, if requested, otherwise default values are
              used.
           5. Perform minimisation.

        Args:
            current_forecast (iris.cube.Cube):
                The cube containing the current forecast.
            historical_forecast (iris.cube.Cube):
                The cube containing the historical forecasts used
                for calibration.
            truth (iris.cube.Cube):
                The cube containing the truth used for calibration.

        Returns:
            (tuple): tuple containing:
                **optimised_coeffs** (dict):
                    Dictionary containing a list of the optimised coefficients
                    for each date.
                **coeff_names** (list):
                    The name of each coefficient.

        """
        # Ensure predictor_of_mean_flag is valid.
        check_predictor_of_mean_flag(self.predictor_of_mean_flag)

        # Setting default values for optimised_coeffs.
        optimised_coeffs = {}

        # Set default values for whether there are NaN values within the
        # initial guess.
        nan_in_initial_guess = False

        date = unit.num2date(
            current_forecast.coord("time").points,
            current_forecast.coord("time").units.name,
            current_forecast.coord("time").units.calendar)[0]

        # Make sure inputs have the same units.
        historic_forecast.convert_units(self.desired_units)
        truth.convert_units(self.desired_units)

        if self.predictor_of_mean_flag.lower() in ["mean"]:
            no_of_realizations = None
            forecast_predictor = historic_forecast.collapsed(
                "realization", iris.analysis.MEAN)
        elif self.predictor_of_mean_flag.lower() in ["realizations"]:
            no_of_realizations = len(
                historic_forecast.coord("realization").points)
            forecast_predictor = historic_forecast

        forecast_var = historic_forecast.collapsed(
            "realization", iris.analysis.VARIANCE)

        # Computing initial guess for EMOS coefficients
        # If no initial guess from a previous iteration, or if there
        # are NaNs in the initial guess, calculate an initial guess.
        if "initial_guess" not in locals() or nan_in_initial_guess:
            initial_guess = self.compute_initial_guess(
                truth, forecast_predictor, self.predictor_of_mean_flag,
                self.ESTIMATE_COEFFICIENTS_FROM_LINEAR_MODEL_FLAG,
                no_of_realizations=no_of_realizations)

        if np.any(np.isnan(initial_guess)):
            nan_in_initial_guess = True

        if not nan_in_initial_guess:
            # Need to access the x attribute returned by the
            # minimisation function.
            optimised_coeffs[date] = (
                self.minimiser.crps_minimiser_wrapper(
                    initial_guess, forecast_predictor,
                    truth, forecast_var,
                    self.predictor_of_mean_flag,
                    self.distribution.lower()))
            initial_guess = optimised_coeffs[date]
        else:
            optimised_coeffs[date] = initial_guess

        return optimised_coeffs


class ApplyCoefficientsFromEnsembleCalibration(object):
    """
    Class to apply the optimised EMOS coefficients to future dates.

    """
    def __init__(
            self, current_forecast, optimised_coeffs, coeff_names,
            predictor_of_mean_flag="mean"):
        """
        Create an ensemble calibration plugin that, for Nonhomogeneous Gaussian
        Regression, applies coefficients created using on historical forecasts
        and applies the coefficients to the current forecast.

        Args:
            current_forecast (iris.cube.Cube):
                The Cube or CubeList containing the current forecast.
            optimised_coeffs (dict):
                Dictionary containing a list of the optimised coefficients
                for each date.
            coeff_names (list):
                The name of each coefficient.
            predictor_of_mean_flag (String):
                String to specify the input to calculate the calibrated mean.
                Currently the ensemble mean ("mean") and the ensemble
                realizations ("realizations") are supported as the predictors.

        """
        self.current_forecast = current_forecast
        self.optimised_coeffs = optimised_coeffs
        self.coeff_names = coeff_names
        self.predictor_of_mean_flag = predictor_of_mean_flag

    def _find_coords_of_length_one(self, cube, add_dimension=True):
        """
        Function to find all coordinates with a length of 1.

        Args:
            cube (iris.cube.Cube):
                Cube
            add_dimension (bool):
                Adds a dimension of 0 to each coordinate. A tuple is appended.

        Returns:
            length_one_coords (list or list of tuples):
                List of length one coordinates or list of tuples containing
                length one coordinates and the dimension.

        """
        length_one_coords = []
        for coord in cube.coords():
            if len(coord.points) == 1:  # Find length one coordinates
                if add_dimension:
                    length_one_coords.append((coord, 0))
                else:
                    length_one_coords.append(coord)
        return length_one_coords

    def _separate_length_one_coords_into_aux_and_dim(
            self, length_one_coords, dim_coords=["time"]):
        """
        Function to separate coordinates into auxiliary and dimension
        coordinates.

        Args:
            length_one_coords (iterable of coordinates):
                The coordinates to be checked for length one coordinates.
            dim_coords (list of coordinates):
                The length one coordinates to be made dimension coordinates.

        Returns:
            (tuple) : tuple containing:
                **length_one_coords_for_aux_coords** (list):
                    List of length one coordinates to be auxiliary coordinates,
                    i.e. not in the dim_coords list.
                    **length_one_coords_for_dim_coords** (list):
                    List of length one coordinates to be dimension coordinates,
                    according to dim_coords list.

        """
        length_one_coords_for_aux_coords = []
        length_one_coords_for_dim_coords = []
        for coord in length_one_coords:
            if coord[0].name() in dim_coords:
                length_one_coords_for_dim_coords.append(coord)
            else:
                length_one_coords_for_aux_coords.append(coord)
        return (
            length_one_coords_for_aux_coords,
            length_one_coords_for_dim_coords)

    def _create_coefficient_cube(
            self, cube, optimised_coeffs_at_date, coeff_names):
        """
        Function to create a cube to store the coefficients used in the
        ensemble calibration.

        Args:
            cube (iterable of coordinates):
                The coordinates to be checked for length one coordinates.
            optimised_coeffs_at_date (list of coefficients):
                Optimised coefficients for a particular date.
            coeff_names (list):
                List of coefficient names.


        Returns:
            coeff_cubes (iris.cube.Cube):
                Cube containing the coefficient value as the data array.

        """
        length_one_coords = self._find_coords_of_length_one(cube)

        length_one_coords_for_aux_coords, length_one_coords_for_dim_coords = (
            self._separate_length_one_coords_into_aux_and_dim(
                length_one_coords))

        coeff_cubes = iris.cube.CubeList([])
        for coeff, coeff_name in zip(optimised_coeffs_at_date, coeff_names):
            cube = iris.cube.Cube(
                [coeff], long_name=coeff_name, attributes=cube.attributes,
                aux_coords_and_dims=length_one_coords_for_aux_coords,
                dim_coords_and_dims=length_one_coords_for_dim_coords)
            coeff_cubes.append(cube)
        return coeff_cubes

    def apply_params_entry(self):
        """
        Wrapping function to calculate the forecast predictor and forecast
        variance prior to applying coefficients to the current forecast.

        Returns:
            (tuple) : tuple containing:
                **calibrated_forecast_predictor** (iris.cube.Cube):
                    Cube containing the calibrated version of the
                    ensemble predictor, either the ensemble mean or
                    the ensemble realizations.
                **calibrated_forecast_variance** (iris.cube.Cube):
                    Cube containing the calibrated version of the
                    ensemble variance, either the ensemble mean or
                    the ensemble realizations.
                **calibrated_forecast_coefficients** (iris.cube.Cube):
                    Cube containing both the coefficients for calibrating
                    the ensemble.

        """
        # Ensure predictor_of_mean_flag is valid.
        check_predictor_of_mean_flag(self.predictor_of_mean_flag)

        if self.predictor_of_mean_flag.lower() in ["mean"]:
            forecast_predictors = self.current_forecast.collapsed(
                "realization", iris.analysis.MEAN)
        elif self.predictor_of_mean_flag.lower() in ["realizations"]:
            forecast_predictors = self.current_forecast
            realization_coeffs = []
            for realization in forecast_predictors.coord("realization").points:
                realization_coeffs.append(
                    "{}{}".format(self.coeff_names[-1], np.int32(realization)))
            self.coeff_names = self.coeff_names[:-1] + realization_coeffs

        forecast_vars = self.current_forecast.collapsed(
            "realization", iris.analysis.VARIANCE)

        (calibrated_forecast_predictor, calibrated_forecast_var,
         calibrated_forecast_coefficients) = self._apply_params(
             forecast_predictors, forecast_vars, self.optimised_coeffs,
             self.coeff_names, self.predictor_of_mean_flag)
        return (calibrated_forecast_predictor,
                calibrated_forecast_var,
                calibrated_forecast_coefficients)

    def _apply_params(
            self, forecast_predictors, forecast_vars, optimised_coeffs,
            coeff_names, predictor_of_mean_flag):
        """
        Function to apply EMOS coefficients to all required dates.

        Args:
            forecast_predictors (iris.cube.Cube):
                Cube containing the forecast predictor e.g. ensemble mean
                or ensemble realizations.
            forecast_vars (iris.cube.Cube):
                Cube containing the forecast variance e.g. ensemble variance.
            optimised_coeffs (dict):
                Coefficients for all dates.
            coeff_names (List):
                Coefficient names.
            predictor_of_mean_flag (str):
                String to specify the input to calculate the calibrated mean.
                Currently the ensemble mean ("mean") and the ensemble
                realizations ("realizations") are supported as the predictors.

        Returns:
            (tuple) : tuple containing:
               **calibrated_forecast_predictor** (iris.cube.Cube):
                    Cube containing the calibrated version of the
                    ensemble predictor, either the ensemble mean or
                    the ensemble realizations.
                **calibrated_forecast_variance** (iris.cube.Cube):
                    Cube containing the calibrated version of the
                    ensemble variance, either the ensemble mean or
                    the ensemble realizations.
                **calibrated_forecast_coefficients** (iris.cube.CubeList):
                    List of cubes containing the coefficients used for
                    calibration.

        """
        date = iris_time_to_datetime(
            forecast_predictors.coord("time").copy())[0]
        if len(optimised_coeffs[date]) != len(coeff_names):
            msg = ("Number of coefficient names {} with names {} "
                   "is not equal to the number of "
                   "optimised_coeffs_at_date values {} "
                   "with values {} or the number of "
                   "coefficients is not greater than the "
                   "number of coefficient names. Can not continue "
                   "if the number of coefficient names out number "
                   "the number of coefficients".format(
                        len(coeff_names), coeff_names,
                        len(optimised_coeffs[date]),
                        optimised_coeffs[date]))
            raise ValueError(msg)
        optimised_coeffs_at_date = dict(
            zip(coeff_names, optimised_coeffs[date]))

        if predictor_of_mean_flag.lower() in ["mean"]:
            # Calculate predicted mean = a + b*X, where X is the
            # raw ensemble mean. In this case, b = beta.
            beta = [optimised_coeffs_at_date["alpha"],
                    optimised_coeffs_at_date["beta"]]
            forecast_predictor_flat = (
                forecast_predictors.data.flatten())
            new_col = np.ones(forecast_predictor_flat.shape,
                              dtype=np.float32)
            all_data = np.column_stack(
                (new_col, forecast_predictor_flat))
            predicted_mean = np.dot(all_data, beta)
            calibrated_forecast_predictor = forecast_predictors
        elif predictor_of_mean_flag.lower() in ["realizations"]:
            # Calculate predicted mean = a + b*X, where X is the
            # raw ensemble mean. In this case, b = beta^2.
            beta_values = np.array([], dtype=np.float32)
            for key in optimised_coeffs_at_date.keys():
                if key.startswith("beta"):
                    beta_values = (
                        np.append(beta_values, optimised_coeffs_at_date[key]))
            beta = np.concatenate(
                [[optimised_coeffs_at_date["alpha"]], beta_values**2])
            forecast_predictor_flat = (
                convert_cube_data_to_2d(forecast_predictors))
            forecast_var_flat = forecast_vars.data.flatten()

            new_col = np.ones(forecast_var_flat.shape, dtype=np.float32)
            all_data = np.column_stack((new_col, forecast_predictor_flat))
            predicted_mean = np.dot(all_data, beta)
            # Calculate mean of ensemble realizations, as only the
            # calibrated ensemble mean will be returned.
            calibrated_forecast_predictor = (
                forecast_predictors.collapsed(
                    "realization", iris.analysis.MEAN))

        xlen = len(forecast_predictors.coord(axis="x").points)
        ylen = len(forecast_predictors.coord(axis="y").points)
        predicted_mean = np.reshape(predicted_mean, (ylen, xlen))
        calibrated_forecast_predictor.data = predicted_mean

        # Calculating the predicted variance, based on the
        # raw variance S^2, where predicted variance = c + dS^2,
        # where c = (gamma)^2 and d = (delta)^2
        predicted_var = (optimised_coeffs_at_date["gamma"]**2 +
                         optimised_coeffs_at_date["delta"]**2 *
                         forecast_vars.data)

        calibrated_forecast_var = forecast_vars
        calibrated_forecast_var.data = predicted_var

        coeff_cubes = self._create_coefficient_cube(
            calibrated_forecast_predictor, optimised_coeffs[date], coeff_names)

        return (calibrated_forecast_predictor,
                calibrated_forecast_var, coeff_cubes)


class EnsembleCalibration(object):
    """
    Plugin to wrap the core EMOS processes:
    1. Estimate optimised EMOS coefficients from training period.
    2. Apply optimised EMOS coefficients for future dates.

    """
    def __init__(self, calibration_method, distribution, desired_units,
                 predictor_of_mean_flag="mean"):
        """
        Create an ensemble calibration plugin that, for Nonhomogeneous Gaussian
        Regression, calculates coefficients based on historical forecasts and
        applies the coefficients to the current forecast.

        Args:
            calibration_method (String):
                The calibration method that will be applied.
                Supported methods are:

                    ensemble model output statistics
                    nonhomogeneous gaussian regression

                Currently these methods are not supported:

                    logistic regression
                    bayesian model averaging

            distribution (String):
                The distribution that will be used for calibration. This will
                be dependent upon the input phenomenon. This has to be
                supported by the minimisation functions in
                ContinuousRankedProbabilityScoreMinimisers.
            desired_units (String or cf_units.Unit):
                The unit that you would like the calibration to be undertaken
                in. The current forecast, historical forecast and truth will be
                converted as required.
            predictor_of_mean_flag (String):
                String to specify the input to calculate the calibrated mean.
                Currently the ensemble mean ("mean") and the ensemble
                realizations ("realizations") are supported as the predictors.
        """
        self.calibration_method = calibration_method
        self.distribution = distribution
        self.desired_units = desired_units
        self.predictor_of_mean_flag = predictor_of_mean_flag

    def __str__(self):
        result = ('<EnsembleCalibration: ' +
                  'calibration_method: {}' +
                  'distribution: {};' +
                  'desired_units: {};' +
                  'predictor_of_mean_flag: {};')
        return result.format(
            self.calibration_method, self.distribution, self.desired_units,
            self.predictor_of_mean_flag)

    def process(self, current_forecast, historic_forecast, truth):
        """
        Performs ensemble calibration through the following steps:
        1. Estimate optimised coefficients from training period.
        2. Apply optimised coefficients to current forecast.

        Args:
            current_forecast (Iris Cube or CubeList):
                The Cube or CubeList that provides the input forecast for
                the current cycle.
            historic_forecast (Iris Cube or CubeList):
                The Cube or CubeList that provides the input historic forecasts
                for calibration.
            truth (Iris Cube or CubeList):
                The Cube or CubeList that provides the input truth for
                calibration with dates matching the historic forecasts.

        Returns:
            (tuple): tuple containing:
                **calibrated_forecast_predictor** (iris.cube.Cube):
                    Cube containing the calibrated forecast predictor.
                **calibrated_forecast_variance** (iris.cube.Cube):
                    Cube containing the calibrated forecast variance.

        """
        def format_calibration_method(calibration_method):
            """Lowercase input string, and replace underscores with spaces."""
            return calibration_method.lower().replace("_", " ")

        # Ensure predictor_of_mean_flag is valid.
        check_predictor_of_mean_flag(self.predictor_of_mean_flag)

        if (format_calibration_method(self.calibration_method) in
                ["ensemble model output statistics",
                 "nonhomogeneous gaussian regression"]):
            if (format_calibration_method(self.distribution) in
                    ["gaussian", "truncated gaussian"]):
                ec = EstimateCoefficientsForEnsembleCalibration(
                    self.distribution, self.desired_units,
                    predictor_of_mean_flag=self.predictor_of_mean_flag)
                optimised_coeffs = (
                    ec.estimate_coefficients_for_ngr(
                        current_forecast, historic_forecast, truth))
        else:
            msg = ("Other calibration methods are not available. "
                   "{} is not available".format(
                       format_calibration_method(self.calibration_method)))
            raise ValueError(msg)
        ac = ApplyCoefficientsFromEnsembleCalibration(
            current_forecast, optimised_coeffs, ec.coeff_names,
            predictor_of_mean_flag=self.predictor_of_mean_flag)
        (calibrated_forecast_predictor, calibrated_forecast_variance,
         calibrated_forecast_coefficients) = ac.apply_params_entry()

        # TODO: track down where np.float64 promotion takes place.
        calibrated_forecast_predictor.data = (
            calibrated_forecast_predictor.data.astype(np.float32))
        calibrated_forecast_variance.data = (
            calibrated_forecast_variance.data.astype(np.float32))

        return calibrated_forecast_predictor, calibrated_forecast_variance
