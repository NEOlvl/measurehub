from measurehub.services.export import raw_csv, result_csv


def test_result_csv_follows_the_stored_column_order_not_dict_order() -> None:
    data = {
        "freq_hz": [1e9, 2e9],
        # a JSONB round trip may return the keys in any order
        "columns": {"mu_im": [0.1, None], "eps_re": [10.0, 11.5]},
        "flags": {"invalid": [False, True]},
        "column_order": ["eps_re", "mu_im"],
        "flag_order": ["invalid"],
    }
    assert result_csv(data) == "freq_hz,eps_re,mu_im,invalid\n1e+09,10,0.1,0\n2e+09,11.5,,1\n"


def test_result_csv_without_stored_order_falls_back_to_dict_order() -> None:
    data = {"freq_hz": [1.0], "columns": {"a": [1.0]}, "flags": {}}
    assert result_csv(data) == "freq_hz,a\n1,1\n"


def test_raw_csv() -> None:
    assert (
        raw_csv([(1.0, 2.0, 3.0, 4.0, 5.0)]) == "freq_hz,s11_re,s11_im,s21_re,s21_im\n1,2,3,4,5\n"
    )
