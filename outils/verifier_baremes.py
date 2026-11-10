#!/usr/bin/env python3
"""Contrôle structurel du flux A3C : ne constitue pas une validation juridique."""
import datetime
import json
import pathlib
import re
import sys

CHEMIN = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path('baremes.json')

def probleme(message):
    print('ERREUR : ' + message, file=sys.stderr)
    raise SystemExit(1)

def date_iso(valeur, champ):
    if not isinstance(valeur, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', valeur):
        probleme(f'{champ} : date attendue au format AAAA-MM-JJ.')
    try:
        datetime.date.fromisoformat(valeur)
    except ValueError:
        probleme(f'{champ} : date calendaire impossible.')
    return valeur

def argent(valeur, champ):
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)) or not 0 <= valeur <= 150:
        probleme(f'{champ} : montant numérique entre 0 et 150 € attendu.')


def main():
    try:
        donnees = json.loads(CHEMIN.read_text(encoding='utf-8'))
    except (OSError, ValueError) as err:
        probleme(f'fichier JSON illisible : {err}')
    if not isinstance(donnees, dict):
        probleme('La racine doit être un objet JSON.')
    attendu = {
        'format': 'A3C-BTP-HDF-BAREMES-1',
        'publisher': 'A3C-VALIDATED',
        'validation': 'JURIDIQUEMENT_CONTROLE',
    }
    for champ, valeur in attendu.items():
        if donnees.get(champ) != valeur:
            probleme(f'{champ} : valeur incompatible avec le simulateur A3C V14.')
    if donnees.get('territory') != 'Hauts-de-France' or donnees.get('idcc') != [1596, 1597]:
        probleme('Territoire ou conventions collectives incompatibles.')
    accords = donnees.get('records')
    if not isinstance(accords, list) or len(accords) > 40:
        probleme('records doit être une liste de 0 à 40 accords.')
    identifiants, dates = set(), set()
    embarques = {'bat2024', 'bat2025', 'bat2026jan', 'bat2026sep'}
    for rang, accord in enumerate(accords, 1):
        if not isinstance(accord, dict):
            probleme(f'Accord {rang} : objet attendu.')
        identifiant = accord.get('id')
        if not isinstance(identifiant, str) or not re.fullmatch(r'[A-Za-z0-9_-]{3,50}', identifiant):
            probleme(f'Accord {rang} : identifiant non conforme.')
        if identifiant in identifiants or identifiant in embarques:
            probleme(f'Identifiant de barème déjà utilisé : {identifiant}.')
        identifiants.add(identifiant)
        libelle = accord.get('libelle')
        if not isinstance(libelle, str) or not 5 <= len(libelle) <= 130:
            probleme(f'{identifiant} : libellé de 5 à 130 caractères attendu.')
        debut = date_iso(accord.get('debut'), f'{identifiant}.debut')
        extension = date_iso(accord.get('extension'), f'{identifiant}.extension')
        if extension < debut:
            probleme(f'{identifiant} : date d’extension antérieure à la date de début.')
        if debut in dates:
            probleme(f'{identifiant} : plusieurs barèmes commencent à la même date (sélection ambiguë).')
        dates.add(debut)
        argent(accord.get('repas'), f'{identifiant}.repas')
        for champ in ('transport', 'trajet'):
            valeurs = accord.get(champ)
            if not isinstance(valeurs, list) or len(valeurs) != 5:
                probleme(f'{identifiant}.{champ} : cinq zones I à V obligatoires.')
            for zone, montant in enumerate(valeurs, 1):
                argent(montant, f'{identifiant}.{champ}[{zone}]')
        source = accord.get('source')
        if not isinstance(source, str) or not source.startswith('https://www.legifrance.gouv.fr/'):
            probleme(f'{identifiant} : URL de source Légifrance nécessaire.')
    print(f'CONFORME : {len(accords)} accord(s) supplémentaire(s), structure compatible avec la V14.')
    print('Attention : ce contrôle technique ne certifie pas les montants, les dates ni l’opposabilité juridique.')

if __name__ == '__main__':
    main()
