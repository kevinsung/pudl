"""A module with functions to aid generating MCOE."""
import pandas as pd

import pudl


def heat_rate_by_unit(pudl_out):
    """
    Calculate heat rates (mmBTU/MWh) within separable generation units.

    Assumes a "good" Boiler Generator Association (bga) i.e. one that only
    contains boilers and generators which have been completely associated at
    some point in the past.

    The BGA dataframe needs to have the following columns:

    - report_date (annual)
    - plant_id_eia
    - unit_id_pudl
    - generator_id
    - boiler_id

    The unit_id is associated with generation records based on report_date,
    plant_id_eia, and generator_id. Analogously, the unit_id is associated
    with boiler fuel consumption records based on report_date, plant_id_eia,
    and boiler_id.

    Then the total net generation and fuel consumption per unit per time period
    are calculated, allowing the calculation of a per unit heat rate. That
    per unit heat rate is returned in a dataframe containing:

    - report_date
    - plant_id_eia
    - unit_id_pudl
    - net_generation_mwh
    - fuel_consumed_mmbtu
    - heat_rate_mmbtu_mwh

    """
    # pudl_out must have a freq, otherwise capacity factor will fail and merges
    # between tables with different frequencies will fail
    if pudl_out.freq is None:
        raise ValueError(
            "pudl_out must include a frequency for heat rate calculation")

    # Sum up the net generation per unit for each time period:
    gen_by_unit = (
        pudl_out.gen_eia923()
        .groupby(['report_date', 'plant_id_eia', 'unit_id_pudl'])
        .agg({'net_generation_mwh': pudl.helpers.sum_na})
        .reset_index()
    )

    # Sum up all the fuel consumption per unit for each time period:
    bf_by_unit = (
        pudl_out.bf_eia923()
        .groupby(['report_date', 'plant_id_eia', 'unit_id_pudl'])
        .agg({'fuel_consumed_mmbtu': pudl.helpers.sum_na})
        .reset_index()
    )

    # Merge together the per-unit generation and fuel consumption data so we
    # can calculate a per-unit heat rate:
    hr_by_unit = (
        pd.merge(
            gen_by_unit,
            bf_by_unit,
            on=['report_date', 'plant_id_eia', 'unit_id_pudl'],
            validate='one_to_one'
        )
        .assign(
            heat_rate_mmbtu_mwh=lambda x: x.fuel_consumed_mmbtu / x.net_generation_mwh
        )
    )

    return pudl.helpers.convert_cols_dtypes(
        hr_by_unit,
        data_source="eia",
        name="hr_by_unit",
    )


def heat_rate_by_gen(pudl_out):
    """
    Convert per-unit heat rate to by-generator, adding fuel type & count.

    Heat rates really only make sense at the unit level, since input fuel and
    output electricity are comingled at the unit level, but it is useful in
    many contexts to have that per-unit heat rate associated with each of the
    underlying generators, as much more information is available about the
    generators.

    To combine the (potentially) more granular temporal information from the
    per-unit heat rates with annual generator level attributes, we have to do
    a many-to-many merge. This can't be done easily with merge_asof(), so we
    treat the year and month fields as categorial variables, and do a normal
    inner merge that broadcasts monthly dates in one direction, and generator
    IDs in the other.

    Returns:
        pandas.DataFrame: with columns report_date, plant_id_eia, unit_id_pudl,
        generator_id, heat_rate_mmbtu_mwh, fuel_type_code_pudl, fuel_type_count.
        The output will have a time frequency corresponding to that of the
        input pudl_out. Output data types are set to their canonical values
        before returning.

    Raises:
        ValueError if pudl_out.freq is None.

    """
    # pudl_out must have a freq, otherwise capacity factor will fail and merges
    # between tables with different frequencies will fail
    if pudl_out.freq is None:
        raise ValueError(
            "pudl_out must include a frequency for heat rate calculation")

    bga_gens = (
        pudl_out.bga_eia860()
        .loc[:, ['report_date', 'plant_id_eia', 'unit_id_pudl', 'generator_id']]
        .drop_duplicates()
        .assign(year=lambda x: x.report_date.dt.year)
        .drop("report_date", axis="columns")
    )

    hr_by_unit = (
        pudl_out.hr_by_unit()
        .assign(year=lambda x: x.report_date.dt.year)
        .loc[:, [
            "year",
            "report_date",
            "plant_id_eia",
            "unit_id_pudl",
            "heat_rate_mmbtu_mwh"
        ]]
    )

    hr_by_gen = (
        pd.merge(
            bga_gens,
            hr_by_unit,
            on=["year", "plant_id_eia", "unit_id_pudl"],
            how="inner",
            validate="many_to_many",
        )
        .loc[:, [
            "report_date",
            "plant_id_eia",
            "unit_id_pudl",
            "generator_id",
            "heat_rate_mmbtu_mwh"
        ]]
    )

    # Bring in generator specific fuel type & fuel count.
    hr_by_gen = pudl.helpers.clean_merge_asof(
        left=hr_by_gen,
        right=pudl_out.gens_eia860()[[
            'report_date',
            'plant_id_eia',
            'generator_id',
            'fuel_type_code_pudl',
            'fuel_type_count'
        ]],
        by={
            "plant_id_eia": "eia",
            "generator_id": "eia",
        }
    )

    return pudl.helpers.convert_cols_dtypes(
        hr_by_gen,
        data_source="eia",
        name="hr_by_gen",
    )


def fuel_cost(pudl_out):
    """
    Calculate fuel costs per MWh on a per generator basis for MCOE.

    Fuel costs are reported on a per-plant basis, but we want to estimate them
    at the generator level. This is complicated by the fact that some plants
    have several different types of generators, using different fuels. We have
    fuel costs broken out by type of fuel (coal, oil, gas), and we know which
    generators use which fuel based on their energy_source_code and reported
    prime_mover. Coal plants use a little bit of natural gas or diesel to get
    started, but based on our analysis of the "pure" coal plants, this amounts
    to only a fraction of a percent of their overal fuel consumption on a
    heat content basis, so we're ignoring it for now.

    For plants whose generators all rely on the same fuel source, we simply
    attribute the fuel costs proportional to the fuel heat content consumption
    associated with each generator.

    For plants with more than one type of generator energy source, we need to
    split out the fuel costs according to fuel type -- so the gas fuel costs
    are associated with generators that have energy_source_code gas, and the
    coal fuel costs are associated with the generators that have
    energy_source_code coal.

    """
    # pudl_out must have a freq, otherwise capacity factor will fail and merges
    # between tables with different frequencies will fail
    if pudl_out.freq is None:
        raise ValueError(
            "pudl_out must include a frequency for fuel cost calculation")

    # Split up the plants on the basis of how many different primary energy
    # sources the component generators have:
    hr_by_gen = (
        pudl_out.hr_by_gen()
        .loc[:, [
            'plant_id_eia',
            'generator_id',
            'unit_id_pudl',
            'report_date',
            'heat_rate_mmbtu_mwh'
        ]]
    )
    gens = (
        pudl_out.gens_eia860()
        .loc[:, [
            'plant_id_eia',
            'report_date',
            'plant_name_eia',
            'plant_id_pudl',
            'generator_id',
            'utility_id_eia',
            'utility_name_eia',
            'utility_id_pudl',
            'fuel_type_count',
            'fuel_type_code_pudl'
        ]]
    )

    # We are inner merging here, which means that we don't get every generator
    # in this output... we only get the ones that show up in hr_by_gen.
    # See Issue #608
    gen_w_ft = pudl.helpers.clean_merge_asof(
        left=hr_by_gen,
        right=gens,
        by={
            "plant_id_eia": "eia",
            "generator_id": "eia",
        }
    )

    one_fuel = gen_w_ft[gen_w_ft.fuel_type_count == 1]
    multi_fuel = gen_w_ft[gen_w_ft.fuel_type_count > 1]

    # Bring the single fuel cost & generation information together for just
    # the one fuel plants:
    one_fuel = pd.merge(
        one_fuel,
        pudl_out.frc_eia923()[[
            'plant_id_eia',
            'report_date',
            'fuel_cost_per_mmbtu',
            'fuel_type_code_pudl',
            'total_fuel_cost',
            'fuel_consumed_mmbtu',
            'fuel_cost_from_eiaapi',
        ]],
        how='left',
        on=['plant_id_eia', 'report_date']
    )
    # We need to retain the different energy_source_code information from the
    # generators (primary for the generator) and the fuel receipts (which is
    # per-delivery), and in the one_fuel case, there will only be a single
    # generator getting all of the fuels:
    one_fuel.rename(columns={'fuel_type_code_pudl_x': 'ftp_gen',
                             'fuel_type_code_pudl_y': 'ftp_frc'},
                    inplace=True)

    # Do the same thing for the multi fuel plants, but also merge based on
    # the different fuel types within the plant, so that we keep that info
    # as separate records:
    multi_fuel = pd.merge(multi_fuel,
                          pudl_out.frc_eia923()[['plant_id_eia',
                                                 'report_date',
                                                 'fuel_cost_per_mmbtu',
                                                 'fuel_type_code_pudl',
                                                 'fuel_cost_from_eiaapi', ]],
                          how='left', on=['plant_id_eia', 'report_date',
                                          'fuel_type_code_pudl'])

    # At this point, within each plant, we should have one record per
    # combination of generator & fuel type, which includes the heat rate of
    # each generator, as well as *plant* level fuel cost per unit heat input
    # for *each* fuel, which we can combine to figure out the fuel cost per
    # unit net electricity generation on a generator basis.

    # We have to do these calculations separately for the single and multi-fuel
    # plants because in the case of the one fuel plants we need to sum up all
    # of the fuel costs -- including both primary and secondary fuel
    # consumption -- whereas in the multi-fuel plants we are going to look at
    # fuel costs on a per-fuel basis (this is very close to being correct,
    # since secondary fuels are typically a fraction of a percent of the
    # plant's overall costs).

    one_fuel_gb = one_fuel.groupby(by=['report_date', 'plant_id_eia'])
    one_fuel_agg = one_fuel_gb.agg({
        'total_fuel_cost': pudl.helpers.sum_na,
        'fuel_consumed_mmbtu': pudl.helpers.sum_na,
        'fuel_cost_from_eiaapi': 'any',
    })
    one_fuel_agg['fuel_cost_per_mmbtu'] = \
        one_fuel_agg['total_fuel_cost'] / \
        one_fuel_agg['fuel_consumed_mmbtu']
    one_fuel_agg = one_fuel_agg.reset_index()
    one_fuel = pd.merge(
        one_fuel[['plant_id_eia', 'report_date', 'generator_id',
                  'heat_rate_mmbtu_mwh', 'fuel_cost_from_eiaapi']],
        one_fuel_agg[['plant_id_eia', 'report_date', 'fuel_cost_per_mmbtu']],
        on=['plant_id_eia', 'report_date'])
    one_fuel = one_fuel.drop_duplicates(
        subset=['plant_id_eia', 'report_date', 'generator_id'])

    multi_fuel = multi_fuel[['plant_id_eia', 'report_date', 'generator_id',
                             'fuel_cost_per_mmbtu', 'heat_rate_mmbtu_mwh',
                             'fuel_cost_from_eiaapi', ]]

    fc = (
        one_fuel.append(multi_fuel, sort=True)
        .assign(
            fuel_cost_per_mwh=lambda x: x.fuel_cost_per_mmbtu * x.heat_rate_mmbtu_mwh
        )
        .sort_values(['report_date', 'plant_id_eia', 'generator_id'])
    )

    out_df = (
        gen_w_ft.drop('heat_rate_mmbtu_mwh', axis=1)
        .drop_duplicates()
        .merge(fc, on=['report_date', 'plant_id_eia', 'generator_id'])
    )

    return pudl.helpers.convert_cols_dtypes(
        out_df,
        data_source="eia",
        name="fuel_cost",
    )


def capacity_factor(pudl_out, min_cap_fact=0, max_cap_fact=1.5):
    """
    Calculate the capacity factor for each generator.

    Capacity Factor is calculated by using the net generation from eia923 and
    the nameplate capacity from eia860. The net gen and capacity are pulled
    into one dataframe, then the dates from that dataframe are pulled out to
    determine the hours in each period based on the frequency. The number of
    hours is used in calculating the capacity factor. Then records with
    capacity factors outside the range specified by min_cap_fact and
    max_cap_fact are dropped.
    """
    # pudl_out must have a freq, otherwise capacity factor will fail and merges
    # between tables with different frequencies will fail
    if pudl_out.freq is None:
        raise ValueError(
            "pudl_out must include a frequency for capacity factor calculation"
        )

    # Only include columns to be used
    gens_eia860 = (
        pudl_out.gens_eia860()
        .loc[:, [
            'plant_id_eia',
            'report_date',
            'generator_id',
            'capacity_mw'
        ]]
    )

    gen = (
        pudl_out.gen_eia923()
        .loc[:, [
            'plant_id_eia',
            'report_date',
            'generator_id',
            'net_generation_mwh'
        ]]
    )

    # merge the generation and capacity to calculate capacity factor
    cf = pudl.helpers.clean_merge_asof(
        left=gen,
        right=gens_eia860,
        by={
            "plant_id_eia": "eia",
            "generator_id": "eia",
        }
    )

    # get a unique set of dates to generate the number of hours
    dates = cf['report_date'].drop_duplicates()
    dates_to_hours = pd.DataFrame(
        data={'report_date': dates,
              'hours': dates.apply(
                  lambda d: (
                      pd.date_range(d, periods=2, freq=pudl_out.freq)[1] -
                      pd.date_range(d, periods=2, freq=pudl_out.freq)[0]) /
                  pd.Timedelta(hours=1))})

    cf = (
        # merge in the hours for the calculation
        cf.merge(dates_to_hours, on=['report_date'])
        # actually calculate capacity factor wooo!
        .assign(
            capacity_factor=lambda x: x.net_generation_mwh / (x.capacity_mw * x.hours)
        )
        # Replace unrealistic capacity factors with NaN
        .pipe(
            pudl.helpers.oob_to_nan,
            ['capacity_factor'],
            lb=min_cap_fact,
            ub=max_cap_fact
        )
        .drop(['hours'], axis=1)
    )

    return pudl.helpers.convert_cols_dtypes(
        cf,
        data_source="eia",
        name="capacity_factor",
    )


def mcoe(
    pudl_out,
    min_heat_rate=5.5,
    min_fuel_cost_per_mwh=0.0,
    min_cap_fact=0.0,
    max_cap_fact=1.5,
    all_gens=True,
):
    """
    Compile marginal cost of electricity (MCOE) at the generator level.

    Use data from EIA 923, EIA 860, and (someday) FERC Form 1 to estimate
    the MCOE of individual generating units. The calculation is performed over
    the range of times and at the time resolution of the input pudl_out object.

    Args:
        pudl_out (pudl.output.pudltable.PudlTabl): a PUDL output object
            specifying the time resolution and date range for which the
            calculations should be performed.
        min_heat_rate (float): lowest plausible heat rate, in mmBTU/MWh. Any
            MCOE records with lower heat rates are presumed to be invalid, and
            are discarded before returning.
        min_cap_fact, max_cap_fact (float): minimum & maximum generator capacity
            factor. Generator records with a lower capacity factor will be
            filtered out before returning. This allows the user to exclude
            generators that aren't being used enough to have valid.
        min_fuel_cost_per_mwh (float): minimum fuel cost on a per MWh basis that
            is required for a generator record to be considered valid. For some
            reason there are now a large number of $0 fuel cost records, which
            previously would have been NaN.
        all_gens (bool): if True, include attributes of all generators in the
            :ref:`generators_eia860` table, rather than just the generators
            which have records in the derived MCOE values. True by default.

    Returns:
        pandas.DataFrame: a dataframe organized by date and generator,
        with lots of juicy information about the generators -- including fuel
        cost on a per MWh and MMBTU basis, heat rates, and net generation.

    """
    gens_idx = ["report_date", "plant_id_eia", "generator_id"]

    # Bring together all derived values we've calculated in the MCOE process:
    mcoe_out = (
        pd.merge(
            pudl_out.fuel_cost()
            .loc[:, gens_idx + [
                "fuel_cost_from_eiaapi",
                "fuel_cost_per_mmbtu",
                "heat_rate_mmbtu_mwh",
                "fuel_cost_per_mwh"
            ]],
            pudl_out.capacity_factor()
            .loc[:, gens_idx + ["net_generation_mwh", "capacity_factor"]],
            on=gens_idx,
            how="outer",
        )
        # Calculate a couple more derived values:
        .assign(
            total_mmbtu=lambda x: x.net_generation_mwh * x.heat_rate_mmbtu_mwh,
            total_fuel_cost=lambda x: x.total_mmbtu * x.fuel_cost_per_mmbtu,
        )
        .pipe(
            pudl.helpers.oob_to_nan,
            ['heat_rate_mmbtu_mwh'],
            lb=min_heat_rate,
            ub=None
        )
        .pipe(
            pudl.helpers.oob_to_nan,
            ['fuel_cost_per_mwh'],
            lb=min_fuel_cost_per_mwh,
            ub=None
        )
        .pipe(
            pudl.helpers.oob_to_nan,
            ['capacity_factor'],
            lb=min_cap_fact,
            ub=max_cap_fact
        )
        # Make sure the merge worked!
        .pipe(
            pudl.validate.no_null_rows,
            df_name="fuel_cost + capacity_factor",
            thresh=0.9
        )
        .pipe(
            pudl.validate.no_null_cols,
            df_name="fuel_cost + capacity_factor"
        )
    )

    # Combine MCOE derived values with all the generator attributes:
    mcoe_out = (
        pd.merge(
            left=(
                pudl_out.gens_eia860()
                .assign(year=lambda x: x.report_date.dt.year)
                .drop("report_date", axis="columns")
            ),
            right=mcoe_out.assign(year=lambda x: x.report_date.dt.year),
            # This "how" determines whether MCOE or gens_eia860 is the backbone
            how="left" if all_gens else "right",
            on=["year", "plant_id_eia", "generator_id"]
        )
        .astype({"year": str})
        .assign(report_date=lambda x: x.report_date.fillna(pd.to_datetime(x.year)))
        .drop("year", axis="columns")
        .pipe(pudl.validate.no_null_rows, df_name="mcoe_all_gens", thresh=0.9)
    )

    # Organize the dataframe for easier legibility
    mcoe_out = (
        mcoe_out.pipe(
            pudl.helpers.organize_cols, [
                'plant_id_eia',
                'generator_id',
                'report_date',
                'unit_id_pudl',
                'plant_id_pudl',
                'plant_name_eia',
                'utility_id_eia',
                'utility_id_pudl',
                'utility_name_eia',
            ])
        .sort_values([
            'plant_id_eia',
            'unit_id_pudl',
            'generator_id',
            'report_date',
        ])
        # Set column data types to canonical values:
        .pipe(
            pudl.helpers.convert_cols_dtypes,
            data_source="eia",
            name="mcoe",
        )
    )

    return mcoe_out
