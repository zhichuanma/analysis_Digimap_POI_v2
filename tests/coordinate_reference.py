"""Independent test oracle; never imported by production code.

OS A Guide to Coordinate Systems in Great Britain V3.6, sections 6.2/6.6
and Annexes A-D:
https://www.ordnancesurvey.co.uk/documents/resources/guide-coordinate-systems-great-britain.pdf
Uses the published *forward* small-angle Helmert formula, not PROJ's inverse
of the reverse parameters. Their millimetre-level difference is intentional.
No PROJ, production imports, grids or inverse-derived reference values.
"""

import math


def os_forward_helmert(x, y, z):
    """OS equation (3), Table 4: ETRS89/WGS84 -> approximate OSGB36 XYZ."""
    rx, ry, rz = (math.radians(v / 3600) for v in (-0.1502, -0.2470, -0.8421))
    scale = 1 + 20.4894e-6
    return (
        -446.448 + scale * x - rz * y + ry * z,
        125.157 + rz * x + scale * y - rx * z,
        -542.060 - ry * x + rx * y + scale * z,
    )


def osgb36_projection(longitude, latitude):
    """OS Annex C.1 series: Airy 1830 longitude/latitude degrees -> BNG m."""
    phi, lam = map(math.radians, (latitude, longitude))
    phi0, lam0 = map(math.radians, (49, -2))
    a, b, f0 = 6377563.396, 6356256.909, 0.9996012717
    e2 = 1 - (b / a) ** 2
    n = (a - b) / (a + b)
    sin_phi, cos_phi, tan_phi = math.sin(phi), math.cos(phi), math.tan(phi)
    nu = a * f0 / math.sqrt(1 - e2 * sin_phi ** 2)
    rho = a * f0 * (1 - e2) / (1 - e2 * sin_phi ** 2) ** 1.5
    eta2 = nu / rho - 1
    m = b * f0 * (
        (1 + n + 5 * n ** 2 / 4 + 5 * n ** 3 / 4) * (phi - phi0)
        - (3 * n + 3 * n ** 2 + 21 * n ** 3 / 8)
        * math.sin(phi - phi0) * math.cos(phi + phi0)
        + (15 * n ** 2 / 8 + 15 * n ** 3 / 8)
        * math.sin(2 * (phi - phi0)) * math.cos(2 * (phi + phi0))
        - 35 * n ** 3 / 24 * math.sin(3 * (phi - phi0)) * math.cos(3 * (phi + phi0))
    )
    ii = nu / 2 * sin_phi * cos_phi
    iii = nu / 24 * sin_phi * cos_phi ** 3 * (5 - tan_phi ** 2 + 9 * eta2)
    iiia = nu / 720 * sin_phi * cos_phi ** 5 * (61 - 58 * tan_phi ** 2 + tan_phi ** 4)
    iv = nu * cos_phi
    v = nu / 6 * cos_phi ** 3 * (nu / rho - tan_phi ** 2)
    vi = nu / 120 * cos_phi ** 5 * (
        5 - 18 * tan_phi ** 2 + tan_phi ** 4 + 14 * eta2 - 58 * tan_phi ** 2 * eta2
    )
    d = lam - lam0
    return (
        400000 + iv * d + v * d ** 3 + vi * d ** 5,
        -100000 + m + ii * d ** 2 + iii * d ** 4 + iiia * d ** 6,
    )


def independent_wgs84_to_bng(longitude, latitude):
    """WGS84 h=0 -> OS forward Helmert -> Airy latitude/longitude -> BNG."""
    phi, lam = map(math.radians, (latitude, longitude))
    a, flattening = 6378137.0, 1 / 298.257223563
    e2 = flattening * (2 - flattening)
    nu = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    xyz = (
        nu * math.cos(phi) * math.cos(lam),
        nu * math.cos(phi) * math.sin(lam),
        nu * (1 - e2) * math.sin(phi),
    )
    x, y, z = os_forward_helmert(*xyz)
    a, b = 6377563.396, 6356256.909
    e2 = 1 - (b / a) ** 2
    p = math.hypot(x, y)
    airy_phi = math.atan2(z, p * (1 - e2))
    for _ in range(30):
        nu = a / math.sqrt(1 - e2 * math.sin(airy_phi) ** 2)
        updated = math.atan2(z + e2 * nu * math.sin(airy_phi), p)
        if abs(updated - airy_phi) < 1e-14:
            airy_phi = updated
            break
        airy_phi = updated
    else:
        raise ArithmeticError("Airy geodetic iteration did not converge")
    return osgb36_projection(math.degrees(math.atan2(y, x)), math.degrees(airy_phi))
