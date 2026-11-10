#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Veille A3C V17 — DILA/KALI et JORF, sans modification de baremes.json.

- Quotidien : complément KALI le plus récent + derniers compléments JORF disponibles.
- Complet / rétroactif : archive KALI globale + derniers JORF (jamais JORF global, 1,6 Go).
- Regroupe les articles d'un même accord, reconstitue des barèmes seulement si non ambigus.
- Signale les arrêtés d'extension pour validation juridique.
- Les montants extraits sont des PROPOSITIONS. Le simulateur n'est jamais mis à jour.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
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
AGENT = 'A3C-Veille-Accords/17 (veille juridique; depot GitHub a3c-baremes-batiment-hdf)'
LIMITE_KALI = 340_000_000
LIMITE_JORF = 125_000_000  # Les JORF globaux sont volontairement exclus
LIMITE_XML = 6_000_000
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
    elif famille == 'JORF':
        quotidiens = sorted(n for n in noms if re.fullmatch(r'JORF_\d{8}-\d{6}\.tar\.gz', n))
        globales = []
    else:
        raise ValueError('Famille de données inconnue.')
    base = SOURCE_KALI if famille == 'KALI' else SOURCE_JORF
    return {
        'quotidiens': [urljoin(base, n) for n in quotidiens[-3:]],
        'global': urljoin(base, globales[-1]) if globales else None,
    }


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
            if tag in ('TITRE_TEXTE', 'TITRE_TXT', 'TITRE', 'INTITULE'):
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


def est_extension_cible(texte: str, titre: str = '') -> bool:
    t = normaliser(texte + ' ' + titre)
    if 'extension' not in t or ('arrete' not in t and 'sont rendues obligatoires' not in t):
        return False
    if 'travaux publics' in t and 'batiment' not in t and not re.search(r'\b(?:1596|1597)\b', t):
        return False
    secteur = 'batiment' in t or bool(re.search(r'\b(?:1596|1597)\b', t))
    region = 'hauts-de-france' in t or 'hauts de france' in t
    # Les arrêtés regroupent parfois plusieurs accords (salaires + déplacement).
    # Retenir l'arrêté régional général si les conventions ciblées sont mentionnées.
    return bool(secteur and region)


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
    if famille == 'JORF':
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
            texte, titre = donnees_xml(flux.read(LIMITE_XML + 1))
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
    resultat = list(groupes.values())
    for g in resultat:
        g['textes_legifrance'] = g['textes_legifrance'][:8]
        if famille == 'KALI' and not g['extraction']:
            g['extraction'] = {'etat': 'non_reconstitue', 'raison': 'Aucun tableau complet et non ambigu repéré',
                               'repas': None, 'transport': None, 'trajet': None, 'date_effet_proposee': None}
    return {'archive': url_archive, 'fichiers_xml_examines': total, 'groupes': resultat}


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
        if g['famille'] == 'JORF':
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
            m = re.search(r'\[A3C:(accord-\d{4}-\d{2}-\d{2}|extension-[\w-]+|titre-[0-9a-f]+)\]', ticket.get('title', ''))
            if m:
                existants.add(m.group(1))
        if len(liste) < 100:
            break
    return existants


def description_ticket(g: dict) -> str:
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


def execution(argv=None) -> int:
    p = argparse.ArgumentParser(description='Veille officielle A3C V17 : KALI + JORF, sans mise à jour automatique des barèmes.')
    p.add_argument('--mode', choices=['quotidien','complet','retroactif'], default='quotidien')
    p.add_argument('--rapport', default='rapport_veille.json')
    p.add_argument('--creer-tickets', action='store_true')
    p.add_argument('--archive-kali-locale', help='Test hors ligne : archive KALI')
    p.add_argument('--archive-jorf-locale', help='Test hors ligne : archive JORF')
    args = p.parse_args(argv)
    erreurs = []
    analyses = []
    try:
        kali = archives_disponibles(lire_index(SOURCE_KALI), 'KALI') if not args.archive_kali_locale else None
        jorf = (archives_disponibles(lire_index(SOURCE_JORF), 'JORF')
                if not args.archive_jorf_locale and args.mode != 'retroactif' else None)
        if args.archive_kali_locale:
            sources_kali = [(args.archive_kali_locale, True)]
        elif args.mode in ('complet', 'retroactif'):
            sources_kali = [(kali['global'], False)] if kali['global'] else []
        else:
            sources_kali = [(url, False) for url in kali['quotidiens'][-1:]]
        if args.archive_jorf_locale:
            sources_jorf = [(args.archive_jorf_locale, True)]
        elif args.mode == 'retroactif':
            sources_jorf = []  # 1,6 Go : historique JORF non téléchargé.
        else:
            sources_jorf = [(url, False) for url in (jorf or {}).get('quotidiens', [])]
        if not sources_kali:
            raise ValueError('Aucune archive KALI disponible.')
        for famille, sources in [('KALI',sources_kali), ('JORF',sources_jorf)]:
            for url, local in sources:
                fichier = url if local else None
                try:
                    if not local:
                        fichier = telecharger(url, LIMITE_KALI if famille == 'KALI' else LIMITE_JORF)
                    analyses.append(analyser_archive(fichier, famille, url if not local else 'Archive locale de test'))
                except (OSError, ValueError, HTTPError, URLError, tarfile.TarError) as ex:
                    erreurs.append(f'{famille} — {url} : {str(ex)[:230]}')
                finally:
                    if not local and fichier:
                        Path(fichier).unlink(missing_ok=True)
        groupes = {}
        for analyse in analyses:
            for groupe in analyse['groupes']:
                if groupe['cle'] not in groupes:
                    groupes[groupe['cle']] = groupe
                else:
                    ancien = groupes[groupe['cle']]
                    ancien['textes_legifrance'] = list(dict.fromkeys(ancien['textes_legifrance'] + groupe['textes_legifrance']))[:8]
                    if ancien['famille'] == 'KALI' and ancien['extraction']['etat'] != 'proposition_a_valider' and groupe['extraction']['etat'] == 'proposition_a_valider':
                        ancien['extraction'] = groupe['extraction']
        ordonnes = list(groupes.values())
        alertes = candidats_nouveaux(ordonnes, args.mode)
        bilan = bilan_temoins(ordonnes) if args.mode != 'quotidien' else {}
        rapport = {
            'version': '17.0', 'horodatage_utc': datetime.now(timezone.utc).isoformat(),
            'mode': args.mode, 'sources': [SOURCE_KALI, SOURCE_JORF],
            'archives': [{'adresse': a['archive'], 'fichiers_xml_examines': a['fichiers_xml_examines'],
                          'documents_regroupes': len(a['groupes'])} for a in analyses],
            'nombre_documents_regroupes': len(ordonnes),
            'nombre_alertes_potentielles': len(alertes),
            'controle_retrospectif': bilan,
            'documents': ordonnes if args.mode != 'quotidien' else alertes,
            'erreurs': erreurs,
            'limites': [
                'Détection non exhaustive, y compris en cas d’absence de résultat.',
                'JORF : contrôle des dernières archives quotidiennes, pas du stock historique de 1,6 Go.',
                'Dates d’effet et d’extension à valider séparément ; extraction des grilles jamais opposable automatiquement.',
                'Aucun accord ni montant n’est ajouté à baremes.json par ce programme.',
            ],
            'baremes_modifies': False,
        }
        Path(args.rapport).write_text(json.dumps(rapport, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('V17 :', len(analyses), 'archives analysées ;', len(ordonnes), 'groupes de textes ;', len(alertes), 'alertes potentielles.')
        for cle, info in bilan.items():
            print('Témoin', cle, ':', info['etat'])
        if args.creer_tickets and args.mode != 'retroactif' and alertes:
            depot, jeton = os.getenv('GITHUB_REPOSITORY',''), os.getenv('GITHUB_TOKEN','')
            if not depot or not jeton:
                erreurs.append('Création des tickets impossible : dépôt ou jeton GitHub absent.')
            else:
                try:
                    nouveaux, doubles = creer_tickets(alertes, depot, jeton)
                    print('Tickets créés :', nouveaux, 'déjà présents :', doubles)
                except (OSError, ValueError, HTTPError, URLError) as ex:
                    erreurs.append('Tickets GitHub : ' + str(ex))
        # Réécriture du rapport si erreur survenue pendant la notification.
        if erreurs:
            rapport['erreurs'] = erreurs
            Path(args.rapport).write_text(json.dumps(rapport, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            for erreur in erreurs:
                print('ATTENTION :', erreur, file=sys.stderr)
            return 1  # Ne pas afficher de coche verte lors d'un contrôle incomplet.
        return 0
    except (OSError, ValueError, HTTPError, URLError, tarfile.TarError) as ex:
        print('ÉCHEC DU PROGRAMME :', ex, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(execution())
