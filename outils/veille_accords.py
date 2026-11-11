#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Veille A3C V20 — surveillance KALI, BOCC et XML JORF/ JORFSIMPLE de la DILA.

La detection des extensions ne depend pas d'une page HTML Legifrance ni d'un identifiant
JORF preinscrit. Le rapport distingue preuve officielle lue et temoins non evaluables.
Le referentiel baremes.json n'est jamais modifie. Les nouveaux barèmes sont à valider.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

SOURCE_KALI = 'https://echanges.dila.gouv.fr/OPENDATA/KALI/'
SOURCE_JORF = 'https://echanges.dila.gouv.fr/OPENDATA/JORF/'
SOURCE_JORFSIMPLE = 'https://echanges.dila.gouv.fr/OPENDATA/JORFSIMPLE/'
SOURCE_BOCC = 'https://echanges.dila.gouv.fr/OPENDATA/BOCC/FluxAnneeCourante/'
BOCC_TEMOIN_2026 = 'CCO20260038.complet.taz'
AGENT = 'A3C-Veille-Accords/20 (veille documentaire DILA; depot GitHub a3c-baremes-batiment-hdf)'
LIMITE_KALI = 340_000_000
LIMITE_JORF = 125_000_000  # Les stocks JORF/JORFSIMPLE > 1 Go sont exclus
LIMITE_XML = 6_000_000
LIMITE_BOCC = 90_000_000
LIMITE_ETAT_ARCHIVES = 250
LIMITE_PDF = 24_000_000
LIMITE_TEXTE = 1_000_000
LIMITE_ALERTES = 12
DERNIER_ACCORD_CONNU = '2026-07-10'
# Témoins historiques non utilisés pour publier un barème, seulement pour tester le moteur.
TEMOINS = {
    '2024-11-25': {'repas': '14.00', 'transport': ['2.68','6.38','9.86','12.96','16.57'], 'trajet': ['1.55','3.11','4.66','6.22','7.78']},
    '2025-12-01': {'repas': '14.00', 'transport': ['2.71','6.44','9.96','13.09','16.74'], 'trajet': ['1.57','3.14','4.71','6.28','7.86']},
    '2026-07-10': {'repas': '14.00', 'transport': ['2.76','6.56','10.14','13.33','17.04'], 'trajet': ['1.57','3.14','4.71','6.28','7.86']},
}
MOIS = {'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5, 'juin': 6,
        'juillet': 7, 'aout': 8, 'septembre': 9, 'octobre': 10, 'novembre': 11, 'decembre': 12}
EUROS = r'(\d{1,3}(?:[\s\u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*(?:€|euros?)?'
ZONES = r'\bzone\s*(?:n[°o]\s*)?([1-5])\b'


def normaliser(texte: str) -> str:
    texte = ''.join(c for c in unicodedata.normalize('NFKD', texte) if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', texte.lower().replace('\u00a0', ' ')).strip()


class Liens(HTMLParser):
    def __init__(self):
        super().__init__()
        self.liens = []

    def handle_starttag(self, nom, attributs):
        if nom == 'a':
            valeur = dict(attributs).get('href')
            if valeur:
                self.liens.append(valeur)


def archives_disponibles(index: str, famille: str) -> dict[str, object]:
    lecteur = Liens()
    lecteur.feed(index)
    noms = [url.rsplit('/', 1)[-1] for url in lecteur.liens]
    if famille == 'KALI':
        quotidiens = sorted(n for n in noms if re.fullmatch(r'KALI_\d{8}-\d{6}\.tar\.gz', n))
        globales = sorted(n for n in noms if re.fullmatch(r'Freemium_kali_global_\d{8}-\d{6}\.tar\.gz', n, re.I))
    elif famille in ('JORF', 'JORFSIMPLE'):
        quotidiens = sorted(n for n in noms if re.fullmatch(rf'{famille}_\d{{8}}-\d{{6}}\.tar\.gz', n))
        globales = []
    else:
        raise ValueError('Famille de données inconnue.')
    base = {'KALI': SOURCE_KALI, 'JORF': SOURCE_JORF, 'JORFSIMPLE': SOURCE_JORFSIMPLE}[famille]
    return {
        'quotidiens': [urljoin(base, n) for n in quotidiens[-15:]],
        'global': urljoin(base, globales[-1]) if globales else None,
    }


def archives_bocc_disponibles(index: str, limite: int = 4) -> dict[str, object]:
    """BOCC : classement par date de mise en ligne, pas par numéro du bulletin.

    La DILA peut mettre en ligne un numéro ancien après un numéro plus récent.
    Les dates proviennent de l'index officiel Apache (approche prudente).
    """
    motif = re.compile(
        r'<a\s+href=["\'](CCO\d{8}\.complet\.taz)["\'][^>]*>[^<]*</a>'
        r'\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', re.I)
    entrees = {nom: date for nom, date in motif.findall(index)}
    lecteur = Liens()
    lecteur.feed(index)
    for lien in lecteur.liens:
        nom = lien.rsplit('/', 1)[-1]
        if re.fullmatch(r'CCO\d{8}\.complet\.taz', nom, re.I):
            entrees.setdefault(nom, '')
    classees = sorted(entrees, key=lambda nom: (entrees[nom], nom))
    return {
        'recents': [urljoin(SOURCE_BOCC, nom) for nom in classees[-limite:]],
        'nombre_total': len(classees),
        'temoin_2026_present': BOCC_TEMOIN_2026 in entrees,
        'index_complet_courant': SOURCE_BOCC,
    }


def qualification_texte_jorf(texte: str, titre: str = '') -> str:
    """Retourne une qualification documentaire, jamais une décision d'opposabilité."""
    t = normaliser(titre + ' ' + texte)
    if not ('arrete' in t and ('extension' in t or 'rendues obligatoires' in t)):
        return 'hors_champ'
    region = bool(re.search(r'hauts[ -]de[ -]france', t))
    batiment = ('batiment' in t or bool(re.search(r'\b1596\b|\b1597\b', t)))
    if not (region and batiment):
        return 'hors_champ'
    if 'petits deplacements' in t or 'petit deplacement' in t:
        return 'extension_deplacements_a_controler'
    return 'extension_regionale_generale_a_examiner'


def est_extension_cible(texte: str, titre: str = '') -> bool:
    return qualification_texte_jorf(texte, titre) != 'hors_champ'


def accords_etendus_dans_texte(texte: str) -> list[dict]:
    """Repère les accords de petits déplacements mentionnés dans un arrêté.

    Repérage par rédaction (« accord ... du ... relatif aux indemnités »), sans
    identifiant connu, ni présomption de date d'extension ou d'applicabilité.
    """
    normalise = normaliser(texte)
    mois = '|'.join(MOIS)
    motif = (r'\baccord(?:s)?\s+(?:regionali?s?\s*)?(?:\([^)]{1,70}\)\s*)?'
             r'(?:.{0,105}?\s+)?du\s+(1er|[0-3]?\d)\s+('
             + mois + r')\s+(20\d{2})\s+relatif(?:s|ve|ves)?\s+aux\s+'
             r'indemnites?\s+de\s+petits?\s+deplacements?')
    dates = set()
    for m in re.finditer(motif, normalise):
        jour, nom, annee = m.group(1), m.group(2), m.group(3)
        try:
            dates.add(datetime(int(annee), MOIS[nom], 1 if jour == '1er' else int(jour)).date().isoformat())
        except ValueError:
            pass
    return [{'date_signature': date, 'objet': 'indemnites_de_petits_deplacements'} for date in sorted(dates)]


def date_publication_xml(blob: bytes) -> str | None:
    """Date officielle explicite dans les métadonnées XML, jamais date du téléchargement."""
    try:
        racine = ET.fromstring(blob)
    except ET.ParseError:
        return None
    for el in racine.iter():
        tag = str(el.tag).rsplit('}', 1)[-1].upper()
        if tag not in ('DATE_PUBLI', 'DATE_PARUTION', 'DATE_PUBLICATION'):
            continue
        valeur = (el.text or '').strip()
        if re.fullmatch(r'20\d{2}-[01]\d-[0-3]\d', valeur):
            try:
                return datetime.strptime(valeur, '%Y-%m-%d').date().isoformat()
            except ValueError:
                continue
        if re.fullmatch(r'20\d{6}', valeur):
            try:
                return datetime.strptime(valeur, '%Y%m%d').date().isoformat()
            except ValueError:
                continue
    return None


def bilan_jorf_temoins(groupes: list[dict], archives_journal: list[dict]) -> dict:
    """Témoin 2026 : uniquement en cas de preuve dans des XML effectivement analysés.

    La date témoin permet de tester le rappel historique, sans identifiant JORF
    préinscrit dans le moteur d'analyse. Une absence de stock ancien est non évaluable.
    """
    correspondants = [g for g in groupes if g.get('famille') in ('JORF', 'JORFSIMPLE')
                       and any(x.get('date_signature') == '2026-07-10'
                               for x in g.get('accords_vises', []))]
    historiques_fournis = any('Archive XML locale' in a.get('archive', '') for a in archives_journal)
    return {'extension_accord_10_juillet_2026': {
        'etat': ('preuve_xml_dila_retrouvee' if any(g.get('preuve_xml_officielle') for g in correspondants) else
                 'correspondance_xml_locale_a_authentifier' if correspondants else
                 'non_retrouve_dans_archives_fournies' if historiques_fournis else
                 'non_evaluable_sans_archive_historique'),
        'documents_officiels': [l for g in correspondants for l in g.get('textes_legifrance', [])],
        'dates_publication_xml': sorted(set(g['date_publication_xml'] for g in correspondants
                                             if g.get('date_publication_xml'))),
        'preuve_archive_xml': any(g.get('preuve_xml_officielle') for g in correspondants),
        'portee': ('Archive XML officielle de la DILA téléchargée et effectivement analysée.'
                   if any(g.get('preuve_xml_officielle') for g in correspondants) else
                   'Correspondance trouvée dans des données XML locales non authentifiées.'
                   if correspondants else
                   'Le flux courant DILA ne donne pas accès aux anciens compléments de la date témoin. '
                   'Aucune preuve de détection historique réelle n’est établie.'),
    }}


def ouvrir(url: str):
    return urlopen(Request(url, headers={'User-Agent': AGENT}), timeout=75)


def lire_index(url: str) -> str:
    with ouvrir(url) as reponse:
        return reponse.read(250_000).decode('latin-1', errors='replace')


def telecharger(url: str, taille_max: int) -> str:
    fichier = tempfile.NamedTemporaryFile(prefix='a3c_veille_', suffix='.tar.gz', delete=False)
    volume = 0
    try:
        with fichier, ouvrir(url) as entree:
            while bloc := entree.read(1024 * 1024):
                volume += len(bloc)
                if volume > taille_max:
                    raise ValueError('Taille de l’archive supérieure à la limite de sécurité.')
                fichier.write(bloc)
        if volume < 100:
            raise ValueError('Archive reçue vide ou tronquée.')
        return fichier.name
    except Exception:
        Path(fichier.name).unlink(missing_ok=True)
        raise


def donnees_xml(blob: bytes) -> tuple[str, str]:
    """Texte lisible et titre. Ne résout pas de ressources XML externes."""
    try:
        racine = ET.fromstring(blob)
        titres = []
        for el in racine.iter():
            tag = el.tag.rsplit('}', 1)[-1].upper() if isinstance(el.tag, str) else ''
            if tag in ('TITRE_TEXTE', 'TITRE_TXT', 'TITRE', 'TITREFULL', 'TITRE_FULL', 'INTITULE'):
                valeur = ' '.join(' '.join(el.itertext()).split())
                if 8 <= len(valeur) <= 350:
                    titres.append(valeur)
        titre = next((v for v in titres if 'haut' in normaliser(v) and
                      ('deplacement' in normaliser(v) or 'accord' in normaliser(v))), '')
        if not titre:
            titre = (titres[0] if titres else '')
        return ' '.join(' '.join(racine.itertext()).split()), titre
    except ET.ParseError:
        brut = blob.decode('utf-8', errors='replace')
        return ' '.join(unescape(re.sub(r'<[^>]+>', ' ', brut)).split()), ''


def date_accord(texte: str) -> str | None:
    """Date de signature extraite d'un titre ou d'une rédaction d'accord."""
    t = normaliser(texte)
    motif = (r'\b(?:accord(?:\s+regional)?|avenant(?:\s+n[°o]?\s*\d+)?)\s+du\s+'
             r'(1er|[0-3]?\d)\s+(' + '|'.join(MOIS) + r')\s+(20\d{2})\b')
    m = re.search(motif, t)
    if not m:
        return None
    try:
        jour = 1 if m.group(1) == '1er' else int(m.group(1))
        return f'{int(m.group(3)):04d}-{MOIS[m.group(2)]:02d}-{jour:02d}'
    except ValueError:
        return None


def est_accord_cible(texte: str, titre: str = '') -> bool:
    t = normaliser(texte + ' ' + titre)
    hdf = 'hauts-de-france' in t or 'hauts de france' in t
    objet = 'petits deplacements' in t or 'petit deplacement' in t
    secteur = (('batiment' in t and ('ouvrier' in t or '1596' in t or '1597' in t))
               or bool(re.search(r'\b(?:1596|1597)\b', t)))
    travaux_publics_seuls = ('travaux publics' in t and 'batiment' not in t
                            and not re.search(r'\b(?:1596|1597)\b', t))
    return bool(hdf and objet and secteur and not travaux_publics_seuls)


def euros(nombre: str) -> str | None:
    try:
        montant = Decimal(nombre.replace('\u00a0', '').replace(' ', '').replace(',', '.'))
        if not Decimal('0.01') <= montant <= Decimal('200'):
            return None
        return str(montant.quantize(Decimal('0.01')))
    except (InvalidOperation, ValueError):
        return None


def valeurs_par_zone(texte: str) -> list[str] | None:
    """Lit cinq entrées 'zone N : X €' sans attribution approximative."""
    motif = re.compile(ZONES + r'\s*[:=\-]?\s*' + EUROS + r'(?!\s*\d)', re.I)
    valeurs: dict[int, str] = {}
    for m in motif.finditer(texte):
        index = int(m.group(1))
        montant = euros(m.group(2))
        if not montant or index in valeurs:
            return None
        valeurs[index] = montant
    if sorted(valeurs) != [1, 2, 3, 4, 5]:
        return None
    return [valeurs[n] for n in range(1, 6)]


def extraire_grille(texte: str) -> dict:
    """Propose une grille uniquement si chaque zone et poste sont non ambigus.

    Gère listes 'Indemnité de transport : zone 1 : ...' et tableaux sur une
    seule ligne 'Zone 1 2,71 € 1,57 €' lorsque l'en-tête transport/trajet est clair.
    Une grille concernant deux sous-régions ou comportant plusieurs périodes est rejetée.
    """
    t = normaliser(texte)
    resultat = {'etat': 'non_reconstitue', 'raison': 'Tableau incomplet ou ambigu',
                'repas': None, 'transport': None, 'trajet': None, 'date_effet_proposee': None}
    # Plusieurs grilles départementales exigent une analyse particulière du territoire.
    if len(re.findall(r'pour les departements\b', t)) > 1:
        resultat['raison'] = 'Plusieurs grilles départementales ; affectation manuelle nécessaire'
        return resultat
    # La date d'effet doit être explicite ; jamais assimilée à la signature de l'accord.
    mois = '|'.join(MOIS)
    matches_dates = re.findall(r'\b(?:au|a compter du|applicable a compter du)\s+'
                               r'(1er|[0-3]?\d)\s+(' + mois + r')\s+(20\d{2})\b', t)
    dates = set()
    for jour, nom, annee in matches_dates:
        dates.add(f'{int(annee):04d}-{MOIS[nom]:02d}-{1 if jour == "1er" else int(jour):02d}')
    if len(dates) == 1:
        resultat['date_effet_proposee'] = next(iter(dates))

    repas = re.findall(r'indemnite\s+de\s+repas.{0,65}?(?:fixee\s+a|est\s+de|:)' \
                      r'\s*(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:€|euros?)', t)
    if len(set(repas)) == 1:
        resultat['repas'] = euros(repas[0])

    # Découpage strict en rubriques, y compris ancien format narratif.
    transport = re.search(r'indemnite\s+de\s+transport\s*:\s*(.*?)(?=indemnite\s+de\s+trajet\s*:|\bpour\s+les\s+departements\b|$)', t)
    trajet = re.search(r'indemnite\s+de\s+trajet\s*:\s*(.*?)(?=\bpour\s+les\s+departements\b|\bconvention\s+collective\b|$)', t)
    if transport and trajet:
        resultat['transport'] = valeurs_par_zone(transport.group(1))
        resultat['trajet'] = valeurs_par_zone(trajet.group(1))
    # Format tableau rédigé en ligne « Indemnité de transport et indemnité de trajet ... Zone 1 X € Y € ».
    if resultat['transport'] is None or resultat['trajet'] is None:
        en_tete = re.search(r'indemnite\s+de\s+transport\s+et\s+indemnite\s+de\s+trajet\s*:', t)
        if en_tete:
            bloc = t[en_tete.end():]
            ligne = re.compile(ZONES + r'\s*[:=\-]?\s*' + EUROS + r'\s*(?:€|euros?)?\s*' + EUROS + r'\s*(?:€|euros?)?', re.I)
            paires = {}
            for m in ligne.finditer(bloc):
                z = int(m.group(1))
                a, b = euros(m.group(2)), euros(m.group(3))
                if z in paires or not a or not b:
                    paires = {}
                    break
                paires[z] = [a, b]
            if sorted(paires) == [1, 2, 3, 4, 5]:
                resultat['transport'] = [paires[z][0] for z in range(1, 6)]
                resultat['trajet'] = [paires[z][1] for z in range(1, 6)]
    # Les trois postes sont nécessaires ; les incohérences bloquent toute proposition.
    if resultat['repas'] and resultat['transport'] and resultat['trajet']:
        tvals = [Decimal(v) for v in resultat['transport']]
        jvals = [Decimal(v) for v in resultat['trajet']]
        if all(tvals[i] < tvals[i + 1] and jvals[i] <= jvals[i + 1] for i in range(4)):
            resultat['etat'] = 'proposition_a_valider'
            resultat['raison'] = 'Extraction structurée non certifiée ; contrôler la source et l’opposabilité.'
        else:
            resultat['raison'] = 'Progression des montants incohérente'
    return resultat


def cle_accord(titre: str, texte: str) -> tuple[str, str | None]:
    date = date_accord(titre) or date_accord(texte[:900])
    # Un même accord IDCC 1596 et IDCC 1597 est regroupé par date de signature.
    # Si date absente, regrouper par titre normalisé, sans y associer un barème automatiquement.
    if date:
        return 'accord-' + date, date
    propre = normaliser(titre)[:130] if titre else normaliser(texte[:150])
    return 'titre-' + hashlib.sha256(propre.encode('utf-8')).hexdigest()[:16], None


def reference(texte: str, chemin: str, famille: str) -> str | None:
    motif = r'KALI(?:TEXT|ARTI)\d{10,}' if famille == 'KALI' else r'JORFTEXT\d{10,}'
    ident = re.search(motif, texte + ' ' + chemin, re.I)
    if not ident:
        return None
    numero = ident.group().upper()
    if famille in ('JORF', 'JORFSIMPLE'):
        return f'https://www.legifrance.gouv.fr/jorf/id/{numero}'
    nature = 'id' if numero.startswith('KALITEXT') else 'article'
    return f'https://www.legifrance.gouv.fr/conv_coll/{nature}/{numero}'


def analyser_archive(chemin: str, famille: str, url_archive: str) -> dict:
    """Flux borné : n'ouvre jamais un chemin extrait du tar sur le disque."""
    groupes = {}
    total = 0
    with tarfile.open(chemin, 'r:gz') as archive:
        for membre in archive:
            if not membre.isfile() or not membre.name.lower().endswith('.xml'):
                continue
            total += 1
            if not 0 < membre.size <= LIMITE_XML:
                continue
            flux = archive.extractfile(membre)
            if flux is None:
                continue
            brut = flux.read(LIMITE_XML + 1)
            texte, titre = donnees_xml(brut)
            pertinent = est_accord_cible(texte, titre) if famille == 'KALI' else est_extension_cible(texte, titre)
            if not pertinent:
                continue
            if famille == 'KALI':
                ident, date = cle_accord(titre, texte)
            else:
                identifiant = re.search(r'JORFTEXT\d{10,}', membre.name + ' ' + texte, re.I)
                nor = re.search(r'\b[A-Z]{4}\d{7}[A-Z]\b', texte)
                ident = 'extension-' + (identifiant.group().upper() if identifiant else (nor.group() if nor else hashlib.sha256((titre + texte[:150]).encode()).hexdigest()[:18]))
                date = None
            if ident not in groupes:
                groupes[ident] = {'cle': ident, 'famille': famille, 'date_signature': date,
                    'titre': titre or 'Texte officiel à contrôler', 'source_archive': url_archive,
                    'textes_legifrance': [], 'fichiers_sources': [], 'extraction': None, 'extrait': ''}
            g = groupes[ident]
            lien = reference(texte, membre.name, famille)
            if lien and lien not in g['textes_legifrance']:
                g['textes_legifrance'].append(lien)
            if len(g['fichiers_sources']) < 12:
                g['fichiers_sources'].append(membre.name)
            if len(g['extrait']) < 650 and len(texte) > len(g['extrait']):
                g['extrait'] = texte[:1400]
            if famille == 'KALI':
                ext = extraire_grille(texte)
                if ext['etat'] == 'proposition_a_valider':
                    # En cas de grilles distinctes au sein d'un accord, bloquer l'affectation.
                    if g['extraction'] and any(g['extraction'].get(p) != ext.get(p) for p in ('repas','transport','trajet')):
                        g['extraction'] = {'etat': 'non_reconstitue', 'raison': 'Plusieurs grilles différentes dans cet accord',
                                           'repas': None, 'transport': None, 'trajet': None, 'date_effet_proposee': None}
                    elif not g['extraction']:
                        g['extraction'] = ext
            else:
                if not g.get('date_publication_proposee'):
                    m = re.search(r'(?:JORF|publication)\s+(?:du\s+)?(\d{1,2}\s+\w+\s+20\d{2})', normaliser(texte))
                    if m:
                        g['date_publication_proposee'] = m.group(1)
                g['objet_extension_a_verifier'] = True
                g['qualification'] = qualification_texte_jorf(texte, titre)
                g['accords_vises'] = list({x['date_signature']: x for x in
                                            g.get('accords_vises', []) + accords_etendus_dans_texte(texte)}.values())
                if not g.get('date_publication_xml'):
                    g['date_publication_xml'] = date_publication_xml(brut)
                g['preuve_xml_officielle'] = (url_archive.startswith(SOURCE_JORF) or
                                            url_archive.startswith(SOURCE_JORFSIMPLE))
    resultat = list(groupes.values())
    for g in resultat:
        g['textes_legifrance'] = g['textes_legifrance'][:8]
        if famille == 'KALI' and not g['extraction']:
            g['extraction'] = {'etat': 'non_reconstitue', 'raison': 'Aucun tableau complet et non ambigu repéré',
                               'repas': None, 'transport': None, 'trajet': None, 'date_effet_proposee': None}
    return {'archive': url_archive, 'fichiers_xml_examines': total, 'groupes': resultat}


def extraire_pdf(blob: bytes) -> tuple[str, str | None]:
    """Extraction textuelle, sans OCR ; renvoie explicitement l'indisponibilité."""
    utilitaire = shutil.which('pdftotext')
    if not utilitaire:
        return '', 'Utilitaire pdftotext absent : pages PDF non analysées.'
    if len(blob) > LIMITE_PDF:
        return '', 'Fichier PDF trop volumineux.'
    with tempfile.NamedTemporaryFile(suffix='.pdf') as f:
        f.write(blob)
        f.flush()
        try:
            resultat = subprocess.run([utilitaire, '-layout', '-enc', 'UTF-8', f.name, '-'],
                                      capture_output=True, timeout=22, check=False)
        except (OSError, subprocess.TimeoutExpired) as erreur:
            return '', 'Conversion PDF impossible : ' + str(erreur)[:100]
    if resultat.returncode:
        return '', 'PDF non exploitable en texte (aucune reconnaissance d’image utilisée).'
    return resultat.stdout.decode('utf-8', errors='replace')[:3_500_000], None


def segments_bocc(nom_fichier: str, brut: bytes) -> tuple[list[tuple[str, str]], str | None]:
    """Divise un bulletin PDF en pages : évite de croiser deux accords différents."""
    if nom_fichier.lower().endswith('.pdf'):
        t, erreur = extraire_pdf(brut)
        if erreur:
            return [], erreur
        # Association prudente des pages ; aucune grille BOCC n'est diffusée automatiquement.
        return [(f'{nom_fichier} — page {num}', page) for num, page in enumerate(t.split('\f'), 1)
                if page.strip()], None
    if nom_fichier.lower().endswith(('.xml', '.html', '.htm')):
        t, titre = donnees_xml(brut)
        return [(titre or nom_fichier, t)], None
    if nom_fichier.lower().endswith('.txt'):
        return [(nom_fichier, brut.decode('utf-8', errors='replace'))], None
    return [], None


def analyser_bocc(chemin: str, url_archive: str) -> dict:
    """Examine les pièces lisibles d'un BOCC ; tout candidat reste à vérifier."""
    groupes = {}
    nombre = pdfs = pieces_lisibles = 0
    avertissements = []

    def traiter_archive(archive):
        nonlocal nombre, pdfs, pieces_lisibles
        for membre in archive:
            if not membre.isfile():
                continue
            nom = membre.name
            extension = nom.lower().rsplit('.', 1)[-1]
            if extension not in ('xml', 'txt', 'pdf', 'html', 'htm'):
                continue
            nombre += 1
            if extension == 'pdf':
                pdfs += 1
            limite = LIMITE_PDF if extension == 'pdf' else LIMITE_XML
            if not 0 < membre.size <= limite:
                avertissements.append('Pièce ignorée pour limite de taille : ' + nom[-90:])
                continue
            flux = archive.extractfile(membre)
            if flux is None:
                continue
            sections, erreur = segments_bocc(nom, flux.read(limite + 1))
            if erreur:
                avertissements.append(nom[-80:] + ' : ' + erreur)
            for titre, texte in sections:
                pieces_lisibles += 1
                if not est_accord_cible(texte, titre):
                    continue
                date = date_accord(texte) or date_accord(titre)
                cle = ('accord-' + date) if date else 'bocc-' + hashlib.sha256(
                    (url_archive + nom + titre).encode('utf-8')).hexdigest()[:18]
                if cle not in groupes:
                    groupes[cle] = {
                        'cle': cle, 'famille': 'BOCC', 'date_signature': date,
                        'titre': titre[:240], 'source_archive': url_archive,
                        'textes_legifrance': [], 'fichiers_sources': [],
                        'extraction': {'etat': 'source_bocc_a_verifier',
                                       'raison': 'Source documentaire BOCC : extraction non certifiée',
                                       'repas': None, 'transport': None, 'trajet': None,
                                       'date_effet_proposee': None},
                        'extrait': texte[:1000], 'qualification': 'accord_deplacements_a_examiner',
                    }
                g = groupes[cle]
                if nom not in g['fichiers_sources'] and len(g['fichiers_sources']) < 9:
                    g['fichiers_sources'].append(nom)
    try:
        # Le suffixe .taz n'indique pas seul le conteneur : tar compressé OU zip.
        if zipfile.is_zipfile(chemin):
            with zipfile.ZipFile(chemin) as z:
                class CompatMembre:
                    def __init__(self, info):
                        self.name, self.size = info.filename, info.file_size
                    def isfile(self):
                        return not self.name.endswith('/')
                class CompatArchive:
                    def __iter__(self):
                        for info in z.infolist():
                            yield CompatMembre(info)
                    def extractfile(self, membre):
                        import io
                        return io.BytesIO(z.read(membre.name))
                traiter_archive(CompatArchive())
        else:
            with tarfile.open(chemin, mode='r:*') as a:
                traiter_archive(a)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as erreur:
        raise ValueError('Archive BOCC illisible : ' + str(erreur)) from erreur
    if nombre and not pieces_lisibles:
        avertissements.append('Archive traitée, mais aucune pièce lisible : veille BOCC incomplète.')
    return {'archive': url_archive, 'fichiers_xml_examines': nombre,
            'fichiers_pdf_examines': pdfs, 'sections_lisibles': pieces_lisibles,
            'avertissements': avertissements[:10], 'groupes': list(groupes.values())}


def bilan_temoins(groupes: list[dict]) -> dict:
    """Contrôle rétrospectif : 2024/2025/2026, sans aucun changement du référentiel."""
    bilan = {}
    for date, attendu in TEMOINS.items():
        correspondants = [g for g in groupes if g.get('date_signature') == date and g['famille'] == 'KALI']
        grilles = [g['extraction'] for g in correspondants if g['extraction']['etat'] == 'proposition_a_valider']
        status = 'introuvable'
        if correspondants:
            status = 'texte_retrouve_grille_non_reconstituee'
        if grilles:
            status = 'conforme' if any(all(gr.get(k) == attendu.get(k) for k in ('repas', 'transport', 'trajet')) for gr in grilles) else 'ecart_a_verifier'
        bilan[date] = {'etat': status, 'nombre_textes_regroupes': len(correspondants),
                       'textes_legifrance': [l for g in correspondants for l in g['textes_legifrance']][:6]}
    return bilan


def candidats_nouveaux(groupes: list[dict], mode: str) -> list[dict]:
    """Historique connu visible au rapport, mais jamais signalé en tickets."""
    liste = []
    for g in groupes:
        if g['famille'] in ('JORF', 'JORFSIMPLE'):
            liste.append(g)
        elif mode == 'quotidien':
            # Un complément daté représente une publication ou correction récente,
            # même si le texte d'accord lui-même est ancien.
            liste.append(g)
        elif g.get('date_signature') and g['date_signature'] > DERNIER_ACCORD_CONNU:
            liste.append(g)
        # En archive globale, ne pas signaler les anciens articles sans date :
        # ils causeraient des alertes historiques sans intérêt.
    return liste


def appel_github(adresse: str, jeton: str, methode='GET', donnees=None):
    charge = json.dumps(donnees, ensure_ascii=False).encode('utf-8') if donnees is not None else None
    demande = Request(adresse, data=charge, method=methode, headers={
        'Authorization': 'Bearer ' + jeton, 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json',
        'User-Agent': AGENT})
    with urlopen(demande, timeout=75) as reponse:
        return json.load(reponse)


def tickets_deja_signales(depot: str, jeton: str) -> set[str]:
    existants = set()
    for page in range(1, 12):
        adresse = f'https://api.github.com/repos/{depot}/issues?state=all&per_page=100&page={page}'
        liste = appel_github(adresse, jeton)
        if not isinstance(liste, list):
            raise ValueError('Liste des tickets GitHub incorrecte.')
        for ticket in liste:
            m = re.search(r'\[A3C:(accord-\d{4}-\d{2}-\d{2}|extension-[\w-]+|bocc-[0-9a-f]+|titre-[0-9a-f]+)\]', ticket.get('title', ''))
            if m:
                existants.add(m.group(1))
        if len(liste) < 100:
            break
    return existants


def description_ticket(g: dict) -> str:
    if g.get('famille') == 'CONTROLE':
        return ('### Incident de couverture de la veille JORF\n\n'
                '**Ce ticket ne signale pas un nouvel accord.**\n\n'
                + g.get('extrait', '') + '\n\n'
                'Contrôle requis : reconstituer la période manquante depuis une source officielle '
                'avant de qualifier la veille de complète. Aucun barème n’a été modifié.')
    x = g.get('extraction') or {}
    parties = ['### Signal officiel détecté — contrôle juridique obligatoire',
       '**Le fichier `baremes.json` n’est jamais modifié par cette automatisation.**',
       f"- Famille : {g['famille']}", f"- Texte : {g['titre']}",
       f"- Date de signature repérée : {g.get('date_signature') or 'Non identifiée'}",
       f"- Archive officielle : {g['source_archive']}",
       '- Sources Légifrance : ' + ', '.join(g.get('textes_legifrance') or ['Non identifiées']),
       '- État de l’extraction : ' + x.get('etat', 'Extension à vérifier'),
       '- Date d’effet proposée : ' + str(x.get('date_effet_proposee') or 'Non établie'),
       '', '| Zone | Repas | Transport | Trajet |', '|---|---:|---:|---:|']
    for i in range(5):
        parties.append(f"| {i+1} | {x.get('repas') or 'À établir'} | "
                       f"{(x.get('transport') or ['—']*5)[i]} | {(x.get('trajet') or ['—']*5)[i]} |")
    parties += ['', '### Contrôles nécessaires',
       '- [ ] Vérifier les IDCC 1596 et/ou 1597 et le territoire Hauts-de-France.',
       '- [ ] Vérifier la nature de l’accord et les organisations signataires.',
       '- [ ] Vérifier les trois indemnités et les cinq zones dans le texte officiel.',
       '- [ ] Vérifier la date de prise d’effet et l’arrêté d’extension au JORF.',
       '- [ ] Vérifier les éventuelles exclusions, réserves ou modifications.',
       '- [ ] Valider juridiquement avant tout ajout manuel au référentiel.',
       '', 'Extrait documentaire (non certifié) :', '> ' + g.get('extrait', '').replace('>', '›')]
    return '\n'.join(parties)[:60000]


def creer_tickets(groupes: list[dict], depot: str, jeton: str) -> tuple[int, int]:
    existants = tickets_deja_signales(depot, jeton)
    nouveaux = doubles = 0
    for g in groupes:
        if g['cle'] in existants:
            doubles += 1
            continue
        if nouveaux >= LIMITE_ALERTES:
            break
        titre = f"[A3C:{g['cle']}] Veille — {g['titre'][:95]}"
        appel_github(f'https://api.github.com/repos/{depot}/issues', jeton, 'POST',
                     {'title': titre, 'body': description_ticket(g)})
        nouveaux += 1
    return nouveaux, doubles


def fusionner_groupes(analyses: list[dict]) -> list[dict]:
    groupes = {}
    for analyse in analyses:
        for groupe in analyse['groupes']:
            cle = (groupe['famille'], groupe['cle'])
            if cle not in groupes:
                groupes[cle] = groupe
            else:
                precedent = groupes[cle]
                precedent['textes_legifrance'] = list(dict.fromkeys(
                    precedent['textes_legifrance'] + groupe['textes_legifrance']))[:8]
                precedent['fichiers_sources'] = list(dict.fromkeys(
                    precedent['fichiers_sources'] + groupe['fichiers_sources']))[:12]
                if groupe['famille'] in ('JORF', 'JORFSIMPLE'):
                    visites = precedent.get('accords_vises', []) + groupe.get('accords_vises', [])
                    precedent['accords_vises'] = list({x['date_signature']: x for x in visites}.values())
                    if groupe.get('date_publication_xml') and not precedent.get('date_publication_xml'):
                        precedent['date_publication_xml'] = groupe['date_publication_xml']
                    precedent['preuve_xml_officielle'] = (precedent.get('preuve_xml_officielle', False)
                                                          or groupe.get('preuve_xml_officielle', False))
                if (groupe['famille'] == 'KALI'
                    and precedent['extraction']['etat'] != 'proposition_a_valider'
                    and groupe['extraction']['etat'] == 'proposition_a_valider'):
                    precedent['extraction'] = groupe['extraction']
    return list(groupes.values())


def charger_etat(chemin: str | None) -> dict:
    etat = {'version': '20.0', 'archives_traitees': {}, 'derniere_date_jorf': None}
    if not chemin or not Path(chemin).is_file():
        return etat
    lu = json.loads(Path(chemin).read_text(encoding='utf-8'))
    if not isinstance(lu, dict) or not isinstance(lu.get('archives_traitees'), dict):
        raise ValueError('Le fichier de suivi de la veille est invalide.')
    for famille, archives in lu['archives_traitees'].items():
        if famille in ('KALI', 'JORF', 'JORFSIMPLE', 'BOCC') and isinstance(archives, list):
            etat['archives_traitees'][famille] = [str(x) for x in archives if isinstance(x, str)][-LIMITE_ETAT_ARCHIVES:]
    date = lu.get('derniere_date_jorf')
    if date is None or re.fullmatch(r'20\d{2}-[01]\d-[0-3]\d', str(date)):
        etat['derniere_date_jorf'] = date
    return etat


def enregistrer_archive(etat: dict, famille: str, url: str) -> None:
    liste = etat['archives_traitees'].setdefault(famille, [])
    if url not in liste:
        liste.append(url)
    etat['archives_traitees'][famille] = liste[-LIMITE_ETAT_ARCHIVES:]
    if famille in ('JORF', 'JORFSIMPLE'):
        m = re.search(r'(?:JORF|JORFSIMPLE)_(20\d{6})-', url)
        if m:
            date = datetime.strptime(m.group(1), '%Y%m%d').date().isoformat()
            if date > (etat.get('derniere_date_jorf') or '0000-00-00'):
                etat['derniere_date_jorf'] = date


def ecarts_de_couverture(etat: dict, adresses: list[str]) -> list[str]:
    """Signale seulement une rupture certaine, et non la simple absence de publication."""
    ancien = etat.get('derniere_date_jorf')
    if not ancien or not adresses:
        return []
    dates = sorted(set(m.group(1) for url in adresses
                       if (m := re.search(r'(?:JORF|JORFSIMPLE)_(20\d{6})-', url))))
    if not dates:
        return []
    try:
        premiere = datetime.strptime(dates[0], '%Y%m%d').date()
        precedente = datetime.strptime(ancien, '%Y-%m-%d').date()
        if (premiere - precedente).days > 2:
            return [f'RUPTURE DE COUVERTURE JORF : dernier flux traité le {ancien} ; '
                    f'première archive encore accessible le {premiere.isoformat()}. '
                    'Certains jours intermédiaires ne sont pas démontrés comme couverts.']
    except ValueError:
        return ['Date de suivi JORF impossible à interpréter.']
    return []


# === V20 : contrôle historique officiel, sans téléchargement d'un stock JORF sur le disque ===
# IMPORTANT : uniquement le mode manuel « temoin » utilise l'archive massive.
# La recherche parcourt librement les XML à partir de leur date de publication :
# aucun identifiant JORFTEXT n'est utilisé pour les sélectionner.

MAX_ARCHIVE_HISTORIQUE_COMPRIMEE = 2_000_000_000
MAX_XML_HISTORIQUES = 8_000_000
DATE_ACCORD_TEMOIN = '2026-07-10'
DATE_PUBLICATION_TEMOIN = '2026-10-31'
ID_JORF_ATTENDU_POUR_CONTROLE = 'JORFTEXT000055037883'  # contrôle A POSTERIORI exclusivement


class FluxBorne:
    """Limite les octets COMPRIMÉS lus ; ne met pas 1,1 Go en mémoire ou disque."""
    def __init__(self, source, maximum=None):
        self.source = source
        self.maximum = MAX_ARCHIVE_HISTORIQUE_COMPRIMEE if maximum is None else maximum
        self.volume = 0

    def read(self, n=-1):
        bloc = self.source.read(n)
        self.volume += len(bloc)
        if self.volume > self.maximum:
            raise ValueError('Stock JORF trop volumineux pour ce contrôle borné.')
        return bloc


def archive_globale_jorf_simple(index: str) -> str | None:
    """Sélection depuis l'index HTTPS officiel, sans URL horodatée imaginée."""
    lecteur = Liens()
    lecteur.feed(index)
    noms = [x.rsplit('/', 1)[-1] for x in lecteur.liens]
    correspondants = sorted(n for n in noms
                           if re.fullmatch(r'Freemium_jorf_simple_20\d{6}-\d{6}\.tar\.gz', n, re.I))
    return urljoin(SOURCE_JORFSIMPLE, correspondants[-1]) if correspondants else None


def dates_xml_metadata(brut: bytes) -> str | None:
    """Utilise DATE_PUBLI de l'XML, sans déduire la date de l'archive."""
    return date_publication_xml(brut)


def confirmation_extension_obligatoire(texte: str, date_accord_cible: str) -> bool:
    """Vérifie qu'un article rend obligatoire l'accord visé : pas seulement un visa « Vu »."""
    t = normaliser(texte)
    for mention in re.finditer(r'\bsont rendues obligatoires\b', t):
        bloc = t[mention.end():mention.end()+1600]
        # La mention d'un AUTRE article ne peut pas servir de preuve.
        limite = re.search(r'\barticle\s+[1-9][0-9]?\b|\bsont rendues obligatoires\b', bloc)
        if limite:
            bloc = bloc[:limite.start()]
        if any(a.get('date_signature') == date_accord_cible
               for a in accords_etendus_dans_texte(bloc)):
            return True
    return False


def qualification_temoin_document(brut: bytes, nom: str, date_cible: str) -> dict | None:
    """Qualifie UNE pièce XML selon les dates et les mentions réellement présentes.

    L'identifiant attendu de 2026 n'entre pas dans la recherche : la validation
    rétrospective le compare à ce qui a été lu (après recherche).
    """
    date_xml = dates_xml_metadata(brut)
    if date_xml != date_cible:
        return None
    texte, titre = donnees_xml(brut)
    if not est_extension_cible(texte, titre):
        return None
    accords = accords_etendus_dans_texte(texte)
    lien = reference(texte, nom, 'JORFSIMPLE')
    ident = re.search(r'JORFTEXT\d{10,}', (lien or '') + ' ' + nom)
    nor = re.search(r'\b[A-Z]{4}\d{7}[A-Z]\b', texte)
    return {
        'titre': titre, 'date_publication_xml': date_xml,
        'qualification': qualification_texte_jorf(texte, titre),
        'accords_vises': accords,
        'accord_temoignage_rendu_obligatoire_dans_article': confirmation_extension_obligatoire(texte, DATE_ACCORD_TEMOIN),
        'lien_legifrance': lien,
        'identifiant_jorf_detecte': ident.group().upper() if ident else None,
        'nor_detecte': nor.group() if nor else None,
        'fichier_archive': nom,
        'empreinte_sha256_xml': hashlib.sha256(brut).hexdigest(),
        'extrait': texte[:1600],
    }


def scanner_stock_jorf_vers_temoins(flux_binaire, date_cible: str, chemin_archive: str,
                                    dossier_preuves: str | None = None,
                                    arret_apres_temoignage: bool = True,
                                    authentifie_dila: bool = False) -> dict:
    """Lit une archive TAR.GZ en flux ; arrêt après preuve de l'accord du 10/07/2026.

    Si le flux est incomplet et qu'aucune preuve n'a été trouvée, la vérification
    échoue explicitement ; aucune absence de publication n'en est déduite.
    """
    date.fromisoformat(date_cible)  # validation de l'entrée
    bornage = FluxBorne(flux_binaire)
    resultat = {'archive_officielle': chemin_archive,
                'acces_dila_direct': bool(authentifie_dila),
                'date_publication_recherchee': date_cible,
                'date_signature_recherchee': DATE_ACCORD_TEMOIN,
                'fichiers_xml_parcourus': 0,
                'fichiers_du_jour_examines': 0,
                'documents_candidates_extension': [],
                'octets_comprimes_lus': 0,
                'archive_parcourue_en_entier': False,
                'arret_apres_preuve': False,
                'preuve_officielle_retrouvee': False,
                'identifiant_controle_a_posteriori': ID_JORF_ATTENDU_POUR_CONTROLE,
                'concordance_identifiant_attendu': None,
                'erreur': None}
    trouve = False
    try:
        with tarfile.open(fileobj=bornage, mode='r|gz', bufsize=256 * 1024) as archive:
            for membre in archive:
                if not membre.isfile() or not membre.name.lower().endswith('.xml'):
                    continue
                resultat['fichiers_xml_parcourus'] += 1
                if resultat['fichiers_xml_parcourus'] > MAX_XML_HISTORIQUES:
                    raise ValueError('Limite du nombre de XML du stock atteinte.')
                if not 0 < membre.size <= LIMITE_XML:
                    continue
                flux = archive.extractfile(membre)
                if flux is None:
                    continue
                brut = flux.read(LIMITE_XML + 1)
                # Préfiltrage par date, jamais par identifiant officiel du témoin.
                iso = date_cible.encode('ascii')
                compact = date_cible.replace('-', '').encode('ascii')
                if iso not in brut and compact not in brut:
                    continue
                date_xml = dates_xml_metadata(brut)
                if date_xml != date_cible:
                    continue
                resultat['fichiers_du_jour_examines'] += 1
                qualif = qualification_temoin_document(brut, membre.name, date_cible)
                if qualif is None:
                    continue
                qualif['preuve_dila'] = bool(authentifie_dila)
                resultat['documents_candidates_extension'].append(qualif)
                correspond = any(a.get('date_signature') == DATE_ACCORD_TEMOIN
                                 for a in qualif['accords_vises'])
                # Une référence JORFTEXT issue de la pièce est requise pour la preuve témoin.
                if (correspond and qualif['identifiant_jorf_detecte']
                        and qualif['accord_temoignage_rendu_obligatoire_dans_article']):
                    trouve = True
                    resultat['concordance_identifiant_attendu'] = (
                        qualif['identifiant_jorf_detecte'] == ID_JORF_ATTENDU_POUR_CONTROLE)
                    if dossier_preuves:
                        racine = Path(dossier_preuves)
                        racine.mkdir(parents=True, exist_ok=True)
                        fichier = racine / ('temoin_jorf_' + date_cible + '.xml')
                        fichier.write_bytes(brut)
                        qualif['piece_xml_conservee'] = str(fichier)
                    if arret_apres_temoignage:
                        resultat['arret_apres_preuve'] = True
                        break
            else:
                resultat['archive_parcourue_en_entier'] = True
    except (OSError, EOFError, tarfile.TarError, ValueError) as erreur:
        resultat['erreur'] = type(erreur).__name__ + ' : ' + str(erreur)[:200]
    resultat['octets_comprimes_lus'] = bornage.volume
    resultat['preuve_officielle_retrouvee'] = bool(trouve and authentifie_dila and not resultat['erreur'])
    return resultat


def verdict_temoin_v20(analyse: dict) -> tuple[str, list[str]]:
    """Statut strict : un simple XML de test ne vaut pas preuve DILA."""
    anomalies = []
    if analyse['erreur']:
        anomalies.append('Lecture interrompue : ' + analyse['erreur'])
        return 'controle_incomplet', anomalies
    if analyse['preuve_officielle_retrouvee']:
        if not analyse.get('concordance_identifiant_attendu'):
            anomalies.append('L’identifiant détecté ne correspond pas à la référence témoin attendue.')
            return 'discordance_a_verifier', anomalies
        return 'preuve_officielle_dila_retrouvee', anomalies
    if analyse.get('concordance_identifiant_attendu') is not None:
        return 'test_local_non_authentifie', ['Archive locale : origine officielle non prouvée.']
    if analyse['archive_parcourue_en_entier']:
        anomalies.append('Le témoin n’a pas été retrouvé dans ce stock ; vérifier le format et le périmètre de la source.')
        return 'temoin_non_retrouve', anomalies
    anomalies.append('Parcours incomplet de l’archive ; aucune conclusion sur l’absence du témoin.')
    return 'controle_incomplet', anomalies


def execution_temoin_v20(args) -> int:
    """Exécution manuelle intensive : ne modifie ni état de suivi ni tickets ni barèmes."""
    horodatage = datetime.now(timezone.utc).isoformat()
    url = None
    try:
        cible = date.fromisoformat(args.date_temoin).isoformat()
    except ValueError:
        cible = args.date_temoin
        erreur_date = 'Date témoin invalide (format attendu AAAA-MM-JJ).'
    else:
        erreur_date = None
    local = args.archive_jorfsimple_locale or args.archive_jorf_locale
    erreur_index = None
    if not local and not erreur_date:
        try:
            url = archive_globale_jorf_simple(lire_index(SOURCE_JORFSIMPLE))
            if not url:
                raise ValueError('Aucune archive globale JORFSIMPLE dans l’index officiel.')
            date_stock = re.search(r'(20\d{6})-\d{6}\.tar\.gz$', url)
            if date_stock and datetime.strptime(date_stock.group(1), '%Y%m%d').date() < date.fromisoformat(cible):
                raise ValueError('Le stock officiel est antérieur à la date du témoin recherché.')
        except (OSError, ValueError) as erreur:
            erreur_index = str(erreur)
    source = ('Archive XML locale fournie pour test' if local else url)
    analyse = None
    if not erreur_date and not erreur_index:
        try:
            contexte = open(local, 'rb') if local else ouvrir(url)
            with contexte as flux:
                analyse = scanner_stock_jorf_vers_temoins(
                    flux, cible, source, dossier_preuves=args.dossier_preuves,
                    authentifie_dila=bool(not local and url and url.startswith(SOURCE_JORFSIMPLE)))
        except (OSError, TimeoutError, ValueError) as erreur:
            erreur_index = str(erreur)
    if analyse is None:
        analyse = {'erreur': erreur_date or erreur_index, 'archive_officielle': source,
                   'preuve_officielle_retrouvee': False, 'archive_parcourue_en_entier': False,
                   'documents_candidates_extension': [], 'fichiers_xml_parcourus': 0,
                   'fichiers_du_jour_examines': 0, 'octets_comprimes_lus': 0,
                   'concordance_identifiant_attendu': None}
    etat, alertes = verdict_temoin_v20(analyse)
    resultat = {
        'version': '20.0', 'mode': 'temoin', 'horodatage_utc': horodatage,
        'etat_controle': etat,
        'date_publication_attendue': cible,
        'date_signature_attendue': DATE_ACCORD_TEMOIN,
        'temoin_jorf': analyse,
        'alertes_controle': alertes,
        'erreurs': [analyse['erreur']] if analyse.get('erreur') else [],
        'baremes_modifies': False,
        'etat_de_veille_modifie': False,
        'mise_en_application': 'Aucune conclusion d’opposabilité sans contrôle juridique de l’arrêté et de l’accord.',
        'limites': [
            'La preuve repose sur un XML effectivement lu dans un flux HTTPS DILA ; aucun certificat ou signature XML indépendante n’est disponible ici.',
            'Le contrôle peut arrêter le parcours après la preuve ; il ne certifie pas l’intégralité du stock.',
            'La référence JORFTEXT attendue est vérifiée après la détection, jamais utilisée pour sélectionner les XML.',
            'Aucune modification du fichier baremes.json ou du fichier suivi/etat_veille.json.',
        ],
    }
    destination = Path(args.rapport)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(resultat, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'V20 témoin historique : {etat} ; {analyse["fichiers_xml_parcourus"]} XML parcourus ; '
          f'{len(analyse["documents_candidates_extension"])} candidats.')
    for x in alertes:
        print('ATTENTION :', x, file=sys.stderr)
    return 0 if etat == 'preuve_officielle_dila_retrouvee' else 2 if etat == 'test_local_non_authentifie' else 1


def execution(argv=None) -> int:
    p = argparse.ArgumentParser(description='Veille A3C V20 : archives XML DILA, sans accès automatisé aux pages HTML Légifrance.')
    p.add_argument('--mode', choices=['quotidien', 'complet', 'retroactif', 'temoin'], default='quotidien')
    p.add_argument('--rapport', default='rapport_veille.json')
    p.add_argument('--etat', help='Fichier de suivi persistant des archives, optionnel en test local')
    p.add_argument('--creer-tickets', action='store_true')
    p.add_argument('--archive-kali-locale')
    p.add_argument('--archive-jorf-locale')
    p.add_argument('--archive-jorfsimple-locale')
    p.add_argument('--archive-bocc-locale')
    p.add_argument('--date-temoin', default='2026-10-31', help='Date JORF témoin (AAAA-MM-JJ) ; mode temoin uniquement')
    p.add_argument('--dossier-preuves', default='preuves_veille', help='Pièces XML de contrôle conservées dans les artifacts, jamais dans baremes.json')
    args = p.parse_args(argv)
    if args.mode == 'temoin':
        return execution_temoin_v20(args)

    erreurs, avertissements, analyses, etat_sources = [], [], [], {}
    try:
        etat = charger_etat(args.etat)
    except (ValueError, OSError, json.JSONDecodeError) as erreur:
        etat = {'version': '20.0', 'archives_traitees': {}, 'derniere_date_jorf': None}
        erreurs.append('Fichier d’état illisible : ' + str(erreur))
    etat_initial = json.dumps(etat, sort_keys=True)
    archives_initiales = sum(len(x) for x in etat.get('archives_traitees', {}).values())
    kali = jorf = bocc = None
    lacunes_jorf = []
    locaux = {'KALI': args.archive_kali_locale, 'JORF': args.archive_jorf_locale,
              'JORFSIMPLE': args.archive_jorfsimple_locale, 'BOCC': args.archive_bocc_locale}
    if not args.archive_kali_locale:
        try:
            kali = archives_disponibles(lire_index(SOURCE_KALI), 'KALI')
            etat_sources['KALI'] = 'index_accessible'
        except (OSError, ValueError) as erreur:
            etat_sources['KALI'] = 'index_indisponible'
            erreurs.append('Index KALI : ' + str(erreur)[:180])
    if not args.archive_jorf_locale and not args.archive_jorfsimple_locale:
        try:
            jorf = archives_disponibles(lire_index(SOURCE_JORFSIMPLE), 'JORFSIMPLE')
            etat_sources['JORFSIMPLE'] = 'index_accessible'
        except (OSError, ValueError) as erreur_simple:
            avertissements.append('JORFSIMPLE inaccessible, bascule vers les compléments XML JORF : ' + str(erreur_simple)[:130])
            try:
                jorf = archives_disponibles(lire_index(SOURCE_JORF), 'JORF')
                etat_sources['JORF'] = 'index_accessible_mode_secours'
            except (OSError, ValueError) as erreur:
                etat_sources['JORF'] = 'index_indisponible'
                erreurs.append('Index JORF et JORFSIMPLE inaccessibles : ' + str(erreur)[:180])
    if not args.archive_bocc_locale:
        try:
            bocc = archives_bocc_disponibles(lire_index(SOURCE_BOCC),
                                               limite=(8 if args.mode != 'quotidien' else 4))
            etat_sources['BOCC'] = 'index_accessible'
        except (OSError, ValueError) as erreur:
            etat_sources['BOCC'] = 'index_indisponible'
            erreurs.append('Index BOCC : ' + str(erreur)[:180])

    def choisir(famille, urls):
        if args.mode == 'retroactif' or not args.etat:
            return [(u, False) for u in urls]
        deja = set(etat['archives_traitees'].get(famille, []))
        return [(u, False) for u in urls if u not in deja]

    kali_sources = ([(args.archive_kali_locale, True)] if args.archive_kali_locale else
                    ([(kali['global'], False)] if kali and kali.get('global') else [])
                    if args.mode != 'quotidien' else
                    choisir('KALI', kali['quotidiens']) if kali else [])
    famille_jorf = ('JORFSIMPLE' if args.archive_jorfsimple_locale or
                    (jorf and etat_sources.get('JORFSIMPLE') == 'index_accessible') else 'JORF')
    if args.archive_jorfsimple_locale:
        jorf_sources = [(args.archive_jorfsimple_locale, True)]
    elif args.archive_jorf_locale:
        jorf_sources = [(args.archive_jorf_locale, True)]
    elif args.mode == 'retroactif':
        jorf_sources = []  # Pas de preuve historique par un flux ne contenant que les derniers jours.
        etat_sources[famille_jorf] = 'historique_non_disponible_dans_flux_courant'
    else:
        adresses_jorf = jorf['quotidiens'] if jorf else []
        lacunes_jorf = ecarts_de_couverture(etat, adresses_jorf)
        avertissements.extend(lacunes_jorf)
        jorf_sources = choisir(famille_jorf, adresses_jorf)
    bocc_sources = ([(args.archive_bocc_locale, True)] if args.archive_bocc_locale else
                    choisir('BOCC', bocc['recents']) if bocc else [])
    # BOCC : une indisponibilité temporaire de Poppler ne doit pas bloquer KALI/JORF.
    # Ne jamais marquer une archive BOCC comme examinée sans lecteur PDF.
    # Le jeu de tests BOCC peut être constitué uniquement de XML : Poppler
    # n'est alors pas nécessaire. Pour les archives distantes réelles,
    # une indisponibilité de pdftotext suspend le traitement BOCC.
    if bocc_sources and not args.archive_bocc_locale and not shutil.which('pdftotext'):
        avertissements.append(
            'SURVEILLANCE PARTIELLE : pdftotext indisponible ; '
            'aucune archive BOCC analysée ou mémorisée lors de cette exécution. '
            'KALI et JORF/JORFSIMPLE restent surveillés. '
            'Relancer le contrôle BOCC après rétablissement du lecteur PDF.')
        etat_sources['BOCC'] = 'suspendu_lecteur_pdf_indisponible'
        bocc_sources = []
    # On ne confond pas « aucune nouvelle archive » et « source en erreur ».
    for famille, sources in [('KALI', kali_sources), (famille_jorf, jorf_sources), ('BOCC', bocc_sources)]:
        if not sources:
            etat_sources[famille] = etat_sources.get(famille) or 'aucun_nouveau_lot'
        for url, local in sources:
            fichier = url if local else None
            try:
                if not local:
                    plafond = LIMITE_KALI if famille == 'KALI' else (LIMITE_BOCC if famille == 'BOCC' else LIMITE_JORF)
                    fichier = telecharger(url, plafond)
                analyse = (analyser_bocc(fichier, url if not local else 'Archive BOCC locale de test')
                           if famille == 'BOCC' else
                           analyser_archive(fichier, famille, url if not local else 'Archive XML locale de test'))
                # Même dans un essai local, une archive BOCC qui contient des PDF
                # ne doit jamais être comptée comme analysée si le lecteur manque.
                if (famille == 'BOCC' and analyse.get('fichiers_pdf_examines', 0)
                        and not shutil.which('pdftotext')):
                    raise ValueError('Archive BOCC incomplète : PDF présents mais lecteur pdftotext indisponible ; lot non mémorisé.')
                analyses.append(analyse)
                etat_sources[famille] = 'archive_analysee'
                if famille == 'BOCC' and analyse.get('fichiers_pdf_examines', 0) and not analyse.get('sections_lisibles', 0):
                    erreurs.append('BOCC : des PDF existent mais aucune section lisible n’a été analysée.')
                avertissements += [famille + ' — ' + x for x in analyse.get('avertissements', [])]
                if not local and args.mode != 'retroactif':
                    enregistrer_archive(etat, famille, url)
            except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as erreur:
                erreurs.append(f'{famille} — {url} : {str(erreur)[:190]}')
            finally:
                if not local and fichier:
                    Path(fichier).unlink(missing_ok=True)

    groupes = fusionner_groupes(analyses)
    nouveaux = candidats_nouveaux(groupes, args.mode)
    for lacune in lacunes_jorf:
        # La lacune est un INCIDENT de contrôle, jamais un accord prétendument nouveau.
        id_lacune = re.search(r'dernier flux traité le (20\d{2}-\d{2}-\d{2})', lacune)
        cle_lacune = 'extension-lacune-' + (id_lacune.group(1).replace('-', '') if id_lacune else
                                          hashlib.sha256(lacune.encode()).hexdigest()[:10])
        nouveaux.append({'cle': cle_lacune, 'famille': 'CONTROLE',
                         'titre': 'Lacune de couverture documentaire JORF',
                         'extrait': lacune, 'source_archive': SOURCE_JORFSIMPLE,
                         'textes_legifrance': [], 'fichiers_sources': [],
                         'date_signature': None, 'extraction': None})
    bilan = bilan_temoins(groupes) if args.mode != 'quotidien' else {}
    temoins_jorf = bilan_jorf_temoins(groupes, analyses) if args.mode == 'retroactif' else {}
    if args.mode == 'retroactif' and temoins_jorf['extension_accord_10_juillet_2026']['etat'] == 'non_evaluable_sans_archive_historique':
        avertissements.append('Témoin JORF du 31 octobre 2026 non évaluable sans archive historique XML officielle. '
                              'Le programme ne télécharge plus les pages HTML Légifrance (HTTP 403).')
    if bocc and not bocc['temoin_2026_present']:
        avertissements.append('BOCC 2026/38 absent de l’index de consultation : aucune conclusion sur sa publication.')
    if jorf and jorf.get('quotidiens') and args.mode != 'retroactif':
        nom = jorf['quotidiens'][-1]
        m = re.search(r'(?:JORF|JORFSIMPLE)_(20\d{6})-', nom)
        if m:
            jour = datetime.strptime(m.group(1), '%Y%m%d').date()
            age = (datetime.now(timezone.utc).date() - jour).days
            if age > 3:
                avertissements.append(f'Dernière archive JORF disponible datée de {age} jours : couverture non garantie.')

    if args.creer_tickets and args.mode != 'retroactif' and nouveaux:
        depot, jeton = os.getenv('GITHUB_REPOSITORY', ''), os.getenv('GITHUB_TOKEN', '')
        if not depot or not jeton:
            erreurs.append('Tickets GitHub impossibles : dépôt ou autorisation absent.')
        else:
            try:
                nb, doubles = creer_tickets(nouveaux, depot, jeton)
                print('Tickets créés :', nb, '; déjà connus :', doubles)
            except (OSError, ValueError) as erreur:
                erreurs.append('Tickets GitHub : ' + str(erreur)[:180])

    rapport = {
        'version': '20.0', 'horodatage_utc': datetime.now(timezone.utc).isoformat(), 'mode': args.mode,
        'sources': [SOURCE_KALI, SOURCE_JORF, SOURCE_JORFSIMPLE, SOURCE_BOCC],
        'etat_sources': etat_sources,
        'archives': [{k: a[k] for k in ('archive', 'fichiers_xml_examines', 'fichiers_pdf_examines', 'sections_lisibles') if k in a}
                     for a in analyses],
        'nombre_documents_regroupes': len(groupes), 'nombre_alertes_potentielles': len(nouveaux),
        'controle_retrospectif': bilan, 'temoins_jorf': temoins_jorf,
        'bocc': {'nombre_bulletins_index': bocc['nombre_total'] if bocc else None,
                 'numero_temoin_2026_38_present': bocc['temoin_2026_present'] if bocc else None},
        'documents': groupes if args.mode != 'quotidien' else nouveaux,
        'erreurs': erreurs, 'avertissements': list(dict.fromkeys(avertissements)),
        'limites': [
            'Sans archive historique JORF fournie, le contrôle rétrospectif ne peut pas certifier un arrêté ancien.',
            'Le flux JORF/JORFSIMPLE courant ne garantit pas de rattrapage si une publication a disparu de l’index.',
            'Les dates de publication sont retenues seulement lorsqu’elles figurent dans les métadonnées XML.',
            'Les archives BOCC étudiées ne constituent pas un historique intégral.',
            'Aucun montant n’est ajouté automatiquement dans baremes.json.',
        ],
        'suivi_persistant': {
            'fichier_demande': args.etat,
            'archives_au_demarrage': archives_initiales,
            'archives_apres_analyse': sum(len(a) for a in etat.get('archives_traitees', {}).values()),
            'ecriture_prevue': bool(args.etat and args.mode != 'retroactif' and not erreurs and json.dumps(etat, sort_keys=True) != etat_initial),
            'confirmation_commit_git': 'à vérifier dans les étapes GitHub Actions',
        },
        'baremes_modifies': False,
    }
    chemin_rapport = Path(args.rapport)
    chemin_rapport.write_text(json.dumps(rapport, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not erreurs and args.etat and args.mode != 'retroactif' and json.dumps(etat, sort_keys=True) != etat_initial:
        dest = Path(args.etat)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(etat, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('V20 :', len(analyses), 'archives lues ;', len(groupes), 'documents ;', len(nouveaux), 'alertes.')
    for nom, temoin in temoins_jorf.items():
        print('Témoin JORF', nom, ':', temoin['etat'])
    if erreurs:
        for erreur in erreurs:
            print('ATTENTION :', erreur, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(execution())
