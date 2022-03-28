"""Auxiliary functions for the quadratic GQTPAR trust-region subsolver."""
import math
from collections import namedtuple

import numpy as np
from scipy.linalg import cho_solve
from scipy.linalg import solve_triangular
from scipy.linalg.lapack import dpotrf as compute_cholesky_factorization
from scipy.optimize._trustregion_exact import estimate_smallest_singular_value


def get_initial_guess_for_lambdas(
    main_model,
):
    """Return good initial guesses for lambda, its lower and upper bound.

    Given a trust-region radius, good initial guesses for the damping factor lambda,
    along with its lower bound and upper bound, are computed.

    The values are chosen accordingly to the guidelines on
    section 7.3.8 (p. 192) from :cite:`Conn2000`.

    Args:
        main_model (namedtuple): Named tuple containing the parameters of the
            main model, i.e.:
            - "linear_terms", a np.ndarray of shape (n,) and
            - "square_terms", a np.ndarray of shape (n,n).

    Returns:
        (dict): Dictionary containing the initial guess for the damping
            factor lambda, along with its lower and upper bound.
            The respective keys are:
            - "candidate"
            - "upper_bound"
            - "lower_bound"
    """
    DampingFactors = namedtuple(
        "DampingFactors", ["candidate", "lower_bound", "upper_bound"]
    )

    gradient_norm = np.linalg.norm(main_model.linear_terms)
    model_hessian = main_model.square_terms

    hessian_infinity_norm = np.linalg.norm(model_hessian, np.Inf)
    hessian_frobenius_norm = np.linalg.norm(model_hessian, "fro")

    hessian_gershgorin_lower, hessian_gershgorin_upper = _compute_gershgorin_bounds(
        main_model
    )

    lambda_lower_bound = max(
        0,
        -min(model_hessian.diagonal()),
        gradient_norm
        - min(hessian_gershgorin_upper, hessian_frobenius_norm, hessian_infinity_norm),
    )
    lambda_upper_bound = max(
        0,
        gradient_norm
        + min(-hessian_gershgorin_lower, hessian_frobenius_norm, hessian_infinity_norm),
    )

    if lambda_lower_bound == 0:
        lambda_candidate = 0
    else:
        lambda_candidate = _get_new_lambda_candidate(
            lower_bound=lambda_lower_bound, upper_bound=lambda_upper_bound
        )

    lambdas = DampingFactors(
        candidate=lambda_candidate,
        lower_bound=lambda_lower_bound,
        upper_bound=lambda_upper_bound,
    )

    return lambdas


def add_lambda_and_factorize_hessian(main_model, hessian_info, lambdas):
    """Add lambda to hessian and factorize it into its upper triangular matrix.

    Args:
        main_model (namedtuple): Named tuple containing the parameters of the
            main model, i.e.:
            - "linear_terms", a np.ndarray of shape (n,) and
            - "square_terms", a np.ndarray of shape (n,n).
        hessian_info (namedtuple): Named tuple containing transformations
            of the hessian, i.e. square_terms, from the main model. The keys are:
            - "hessian_plus_lambda" (np.ndarray): The square terms of the main model
                plus the identity matrix times lambda.
            - "upper_triangular" (np.ndarray): Factorization of the hessian from the
                main model into its upper triangular matrix.
            - "info_already_factorized" (bool): Boolean indicating whether the hessian
                has already been factorized for the current iteration.

    Returns:
        Tuple:
        - hessian_info (dict): Named tuple containing the updated transformations
            of the hessian, i.e. square_terms, from the main model. See above.
        - factorization_info (int): Non-negative integer k indicating whether the
            factorization of the hessian into its upper triangular matrix has been
            successful.
            If k = 0, the factorization has been successful.
            A value k > 0 means that the leading k by k submatrix constitues the
            first non-positive definite leading submatrix of the hessian.
    """
    n = main_model.square_terms.shape[0]

    hessian_plus_lambda = main_model.square_terms + lambdas.candidate * np.eye(n)
    hessian_upper_triangular, factorization_info = compute_cholesky_factorization(
        hessian_plus_lambda,
        lower=False,
        overwrite_a=False,
        clean=True,
    )

    hessian_info_new = hessian_info._replace(
        hessian_plus_lambda=hessian_plus_lambda,
        upper_triangular=hessian_upper_triangular,
    )

    return hessian_info_new, factorization_info


def find_new_candidate_and_update_parameters(
    main_model,
    hessian_info,
    lambdas,
    stopping_criteria,
    converged,
):
    """Find new candidate vector and update transformed hessian and lambdas."""
    x_candidate = cho_solve(
        (hessian_info.upper_triangular, False), -main_model.linear_terms
    )
    x_norm = np.linalg.norm(x_candidate)

    if x_norm <= 1 and lambdas.candidate == 0:
        converged = True

    w = solve_triangular(hessian_info.upper_triangular, x_candidate, trans="T")
    w_norm = np.linalg.norm(w)

    newton_step = _compute_newton_step(lambdas, x_norm, w_norm)

    if x_norm < 1:
        (
            x_candidate,
            hessian_info,
            lambdas_new,
            converged,
        ) = _update_candidate_and_parameters_when_candidate_within_trustregion(
            x_candidate,
            main_model,
            hessian_info,
            lambdas,
            newton_step,
            stopping_criteria,
            converged,
        )

    else:
        lambdas_new, converged = _update_lambdas_when_candidate_outside_trustregion(
            lambdas,
            newton_step,
            x_norm,
            stopping_criteria,
            converged,
        )

    return (
        x_candidate,
        hessian_info,
        lambdas_new,
        converged,
    )


def check_for_interior_convergence_and_update(
    x_candidate,
    model_hessian,
    lambdas,
    stopping_criteria,
    converged,
):
    """Check for interior convergence, update candidate vector and lambdas."""
    n = x_candidate.shape[0]

    if lambdas.candidate == 0:
        x_candidate = np.zeros(n)
        converged = True

    s_min, z_min = estimate_smallest_singular_value(model_hessian["upper_triangular"])
    step_len = 2

    if step_len**2 * s_min**2 <= stopping_criteria["k_hard"] * lambdas["current"]:
        x_candidate = step_len * z_min
        converged = True

    lambda_lower_bound = max(lambdas.lower_bound, lambdas.upper_bound - s_min**2)
    lambda_new_candidate = _get_new_lambda_candidate(
        lower_bound=lambda_lower_bound, upper_bound=lambdas.candidate
    )

    lambdas_new = lambdas._replace(
        candidate=lambda_new_candidate,
        lower_bound=lambda_lower_bound,
        upper_bound=lambdas.candidate,
    )

    return x_candidate, lambdas_new, converged


def update_lambdas_when_factorization_unsuccessful(
    hessian_info, lambdas, factorization_info
):
    """Update lambdas in the case that factorization of hessian not successful."""
    delta, v = _compute_terms_to_make_leading_submatrix_singular(
        hessian_info,
        factorization_info,
    )
    v_norm = np.linalg.norm(v)

    lambda_lower_bound = max(
        lambdas.lower_bound, lambdas.candidate + delta / v_norm**2
    )
    lambda_new_candidate = _get_new_lambda_candidate(
        lower_bound=lambda_lower_bound, upper_bound=lambdas.upper_bound
    )

    lambdas_new = lambdas._replace(
        candidate=lambda_new_candidate,
        lower_bound=lambda_lower_bound,
    )

    return lambdas_new


def evaluate_model_criterion(x, main_model):
    """Evaluate the criterion function value of the main model.

    Args:
        x (np.ndarray): Parameter vector of shape (n,).
        main_model (namedtuple): Named tuple containing the parameters of the
            main model, i.e.:
            - "linear_terms", a np.ndarray of shape (n,) and
            - "square_terms", a np.ndarray of shape (n,n).

    Returns:
        (float): Criterion value of the main model.
    """
    return np.dot(main_model.linear_terms, x) + 0.5 * np.dot(
        np.dot(x, main_model.square_terms), x
    )


def _get_new_lambda_candidate(lower_bound, upper_bound):
    """Update current lambda so that it lies within its bounds.

    Args:
        lambdas_new (namedtuple): Named tuple containing the current candidate
            value for the damping factor lambda, its lower bound and upper bound.

    Returns:
        lambda_new_candidate (float): New candidate for the damping factor
            lambda.
    """
    lambda_new_candidate = max(
        math.sqrt(lower_bound * upper_bound),
        lower_bound + 0.01 * (upper_bound - lower_bound),
    )

    return lambda_new_candidate


def _compute_gershgorin_bounds(main_model):
    """Compute upper and lower Gregoshgorin bounds for a square matrix.

    The Gregoshgorin bounds are the upper and lower bounds for the
    eigenvalues of the square hessian matrix (i.e. the square terms of
    the main model). See :cite:`Conn2000`.

    Args:
        main_model (namedtuple): Named tuple containing the parameters of the
            main model, i.e.:
            - "linear_terms", a np.ndarray of shape (n,) and
            - "square_terms", a np.ndarray of shape (n,n).

    Returns:
        Tuple:
        - lower_bound (float): Lower Gregoshgorin bound.
        - upper_bound (float): Upper Gregoshgorin bound.
    """
    model_hessian = main_model.square_terms

    hessian_diag = np.diag(model_hessian)
    hessian_diag_abs = np.abs(hessian_diag)
    hessian_row_sums = np.sum(np.abs(model_hessian), axis=1)

    lower_gershgorin = np.min(hessian_diag + hessian_diag_abs - hessian_row_sums)
    upper_gershgorin = np.max(hessian_diag - hessian_diag_abs + hessian_row_sums)

    return lower_gershgorin, upper_gershgorin


def _compute_newton_step(lambdas, p_norm, w_norm):
    """Compute the Newton step.

    Args:
        lambdas (namedtuple): Named tuple containing the current candidate
            value for the damping factor lambda, its lower bound and upper bound.
        p_norm (float): Frobenius (i.e. L2-norm) of the candidate vector.
        w_norm (float): Frobenius (i.e. L2-norm) of vector w, which is the solution
            to the following triangular system: U.T w = p.

    Returns:
        (float): Newton step computed according to formula (4.44) p.87
            from Nocedal and Wright (2006).
    """
    return lambdas.candidate + (p_norm / w_norm) ** 2 * (p_norm - 1)


def _update_candidate_and_parameters_when_candidate_within_trustregion(
    x_candidate,
    main_model,
    hessian_info,
    lambdas,
    newton_step,
    stopping_criteria,
    converged,
):
    """Update candidate vector, hessian, and lambdas when x outside trust-region."""
    n = main_model.square_terms.shape[0]

    s_min, z_min = estimate_smallest_singular_value(hessian_info.upper_triangular)
    step_len = _compute_smallest_step_len_for_candidate_vector(x_candidate, z_min)

    quadratic_term = np.dot(
        x_candidate, np.dot(hessian_info.hessian_plus_lambda, x_candidate)
    )

    relative_error = (step_len**2 * s_min**2) / (quadratic_term + lambdas.candidate)
    if relative_error <= stopping_criteria["k_hard"]:
        x_candidate = x_candidate + step_len * z_min
        converged = True

    lambda_new_lower_bound = max(lambdas.lower_bound, lambdas.candidate - s_min**2)

    hessian_plus_lambda = main_model.square_terms + newton_step * np.eye(n)
    _, factorization_unsuccessful = compute_cholesky_factorization(
        hessian_plus_lambda,
        lower=False,
        overwrite_a=False,
        clean=True,
    )

    if factorization_unsuccessful == 0:
        hessian_already_factorized = True
        lambda_new_candidate = newton_step
    else:
        hessian_already_factorized = hessian_info.already_factorized
        lambda_new_lower_bound = max(lambda_new_lower_bound, newton_step)
        lambda_new_candidate = _get_new_lambda_candidate(
            lower_bound=lambda_new_lower_bound, upper_bound=lambdas.candidate
        )

    hessian_info_new = hessian_info._replace(
        hessian_plus_lambda=hessian_plus_lambda,
        already_factorized=hessian_already_factorized,
    )

    lambdas_new = lambdas._replace(
        candidate=lambda_new_candidate,
        lower_bound=lambda_new_lower_bound,
        upper_bound=lambdas.candidate,
    )

    return x_candidate, hessian_info_new, lambdas_new, converged


def _update_lambdas_when_candidate_outside_trustregion(
    lambdas, newton_step, p_norm, stopping_criteria, converged
):
    """Update lambas in the case that candidate vector lies outside trust-region."""
    relative_error = abs(p_norm - 1)

    if relative_error <= stopping_criteria["k_easy"]:
        converged = True

    lambdas_new = lambdas._replace(candidate=newton_step, lower_bound=lambdas.candidate)

    return lambdas_new, converged


def _compute_smallest_step_len_for_candidate_vector(x_candidate, z_min):
    """Compute the smallest step length for the candidate vector.

    Choose step_length with the smallest magnitude.
    The reason for this choice is explained at p. 6 in :cite:`More1983`,
    just before the formula for tau.

    Args:
        x_candidate (np.ndarray): Candidate vector of shape (n,).
        z_min (float): Smallest singular value of the hessian matrix.

    Returns:
        (float) Step length with the smallest magnitude.
    """
    ta, tb = _solve_scalar_quadratic_equation(x_candidate, z_min)
    step_len = min([ta, tb], key=abs)

    return step_len


def _solve_scalar_quadratic_equation(z, d):
    """Return the sorted values that solve the scalar quadratic equation.

    Solve the scalar quadratic equation ||z + t d|| == trustregion_radius.
    This is like a line-sphere intersection.


    Computation of the ``aux`` step, ``ta`` and ``tb`` is mathematically equivalent
    to equivalent the following calculation:

    ta = (-b - sqrt_discriminant) / (2*a)
    tb = (-b + sqrt_discriminant) / (2*a)

    but produces smaller round-off errors.
    For more details, look at "Matrix Computation" p.97.

    Args:
        z (np.ndarray): Eigenvector of the upper triangular hessian matrix.
        d (float): Smallest singular value of the upper triangular of the
            hessian matrix.

    Returns
        Tuple: The two values of t, sorted from low to high.
        - (float) Lower value of t.
        - (float) Higher value of t.
    """
    a = np.dot(d, d)
    b = 2 * np.dot(z, d)
    c = np.dot(z, z) - 1
    sqrt_discriminant = math.sqrt(b * b - 4 * a * c)

    aux = b + math.copysign(sqrt_discriminant, b)
    ta = -aux / (2 * a)
    tb = -2 * c / aux

    return sorted([ta, tb])


def _compute_terms_to_make_leading_submatrix_singular(hessian_info, k):
    """Compute terms that make the leading submatrix of the hessian singular.

    The "hessian" here refers to the matrix

        H + lambda * I(n),

    where H is the initial hessian, lambda is the current damping factor,
    I the identity matrix, and m the number of rows/columns of the symmetric
    hessian matrix.

    Args:
        hessian (np.ndarray): Symmetric k by k hessian matrix, which is not
            positive definite.
        upper_triangular (np.ndarray) Upper triangular matrix resulting of an
            incomplete Cholesky decomposition of the hessian matrix.
        k (int): Positive integer such that the leading k by k submatrix from
            hessian is the first non-positive definite leading submatrix.

    Returns:
        tuple:
        - delta(float): Amount that should be added to the element (k, k) of
            the leading k by k submatrix of the hessian to make it singular.
        - v (np.ndarray): A vector such that ``v.T B v = 0``. Where B is the
            hessian after ``delta`` is added to its element (k, k).
    """
    hessian_plus_lambda = hessian_info.hessian_plus_lambda
    upper_triangular = hessian_info.upper_triangular
    n = len(hessian_plus_lambda)

    delta = (
        np.sum(upper_triangular[: k - 1, k - 1] ** 2)
        - hessian_plus_lambda[k - 1, k - 1]
    )

    v = np.zeros(n)
    v[k - 1] = 1

    if k != 1:
        v[: k - 1] = solve_triangular(
            upper_triangular[: k - 1, : k - 1], -upper_triangular[: k - 1, k - 1]
        )

    return delta, v