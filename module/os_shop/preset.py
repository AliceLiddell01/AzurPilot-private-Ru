"""Предустановленные конфигурации магазина Operation Siren.

Определяет приоритеты товаров при различных стратегиях покупки.
Категории строк:
    1: Координаты (логгеры), очки действия, фиолетовые монеты
    2: Предметы рангов T0 и T1
    3: Прочие предметы
    4: Предметы META
    5: Предметы низкой ценности
"""

OS_SHOP = {
    'max_benefit': """
        LoggerAbyssalT6 > LoggerAbyssalT5 > LoggerObscure > LoggerAbyssalT4 > ActionPoint > PurpleCoins >
        GearDesignPlanT3 > PlateRandomT4 > DevelopmentMaterialT3 > GearDesignPlanT2 > GearPart >
        OrdnanceTestingReportT3 > OrdnanceTestingReportT2 > DevelopmentMaterialT2 > OrdnanceTestingReportT1
    """,
    'max_benefit_meta': """
        LoggerAbyssalT6 > LoggerAbyssalT5 > LoggerObscure > LoggerAbyssalT4 > ActionPoint > PurpleCoins >
        GearDesignPlanT3 > PlateRandomT4 > DevelopmentMaterialT3 > GearDesignPlanT2 > GearPart >
        OrdnanceTestingReportT3 > OrdnanceTestingReportT2 > DevelopmentMaterialT2 > OrdnanceTestingReportT1 >
        METARedBook > CrystallizedHeatResistantSteel > NanoceramicAlloy > NeuroplasticProstheticArm > SupercavitationGenerator
    """,
    'no_meta': """
        LoggerAbyssalT6 > LoggerAbyssalT5 > LoggerObscure > LoggerAbyssalT4 > ActionPoint > PurpleCoins >
        GearDesignPlanT3 > PlateRandomT4 > DevelopmentMaterialT3 > GearDesignPlanT2 > GearPart >
        OrdnanceTestingReportT3 > OrdnanceTestingReportT2 > DevelopmentMaterialT2 > OrdnanceTestingReportT1 >
        LoggerAbyssalT3 > DevelopmentMaterialT1 > TuningT2 > TuningSample > EnergyStorageDevice > RepairPack
    """,
    'all': """
        LoggerAbyssalT6 > LoggerAbyssalT5 > LoggerObscure > LoggerAbyssalT4 > ActionPoint > PurpleCoins >
        GearDesignPlanT3 > PlateRandomT4 > DevelopmentMaterialT3 > GearDesignPlanT2 > GearPart >
        OrdnanceTestingReportT3 > OrdnanceTestingReportT2 > DevelopmentMaterialT2 > OrdnanceTestingReportT1 >
        METARedBook > CrystallizedHeatResistantSteel > NanoceramicAlloy > NeuroplasticProstheticArm > SupercavitationGenerator >
        LoggerAbyssalT3 > DevelopmentMaterialT1 > TuningT2 > TuningSample > EnergyStorageDevice > RepairPack
    """
}
